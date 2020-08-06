from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    connection: serial
    short_description: execute on a serial device
    description:
        - This connection plugin allows ansible to execute tasks over a serial device.
    author: Charles Durieux (charles-durieux@negentropy.in.net)
    version_added: None
    options:
      baudrate:
        description: Serial connetion baudrate
        default: 115200
        ini:
          - section: defaults
            key: baudrate
        env:
          - name: ANSIBLE_SERIAL_BAUDRATE
        vars:
          - name: ansible_serial_baudrate
      host:
        description: Hostname of the remote machine
        default: inventory_hostname
        vars:
          - name: ansible_host
      serial_port:
        description: Serial port to connect to
        default: /dev/ttyS0
        ini:
          - section: defaults
            key: remote_serial_port
        env:
          - name: ANSIBLE_REMOTE_SERIAL_PORT
        vars:
          - name: ansible_serial_port
      payload_size:
        description:
          - bytesize of payloads on write channel
        default: 512
        ini:
          - section: defaults
            key: payload_size
        env:
          - name: ANSIBLE_SERIAL_PAYLOAD_SIZE
        vars:
          - name: ansible_serial_payload_size
      remote_user:
        description:
          - User name with which to login to the remote server, normally set by the remote_user keyword
          - If no user is supplied, root is used
        default: root
        ini:
          - section: defaults
            key: remote_user
        env:
          - name: ANSIBLE_REMOTE_USER
        vars:
          - name: ansible_user
'''
import base64
import dataclasses
import io
import queue
import re
import serial
import threading
import time

import ansible.constants as C
from ansible.plugins.connection import ConnectionBase
from ansible.errors import AnsibleError
from ansible.utils.display import Display

display = Display()

# TODO remove message dataclass
@dataclasses.dataclass
class Message:
    '''Message to use in write queue'''
    data: 'typing.Any'
    is_raw: bool = False

class Connection(ConnectionBase):
    ''' Serial based connections '''

    transport = 'serial'
    has_pipelining = False

    # sleep interval for loops (to no detroy cpu), in seconds
    loop_interval = 0.05

    # seconds to wait if the response is not what we expect
    read_timeout = 5

    def __init__(self, *args, **kwargs):

        super(Connection, self).__init__(*args, **kwargs)

        user = self._play_context.remote_user
        self.user = user if user else 'root'

        passwd = self._play_context.password
        self.passwd = passwd if passwd else ''

        self.host = self._play_context.remote_addr

        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

        self.ser = serial.Serial()

        self.is_connected = False
        self.ps1 = None
        # TODO improve clarity
        self.q = {a: queue.Queue() for a in ['read', 'write']}

    def __del__(self):
        if isinstance(self.stdout, io.BytesIO):
            self.stdout.close()
        if isinstance(self.stderr, io.BytesIO):
            self.stderr.close()

    def _connect(self):
        ''' connect to the serial device '''

        if not self.is_connected:
            # get serial connection parameters
            self.ser.port = self.get_option('serial_port')
            self.ser.baudrate = self.get_option('baudrate')
            self.payload_size = int(self.get_option('payload_size'))
            self.ser.timeout = 0

            # initiate serial connection
            self.ser.open()
            self.is_connected = True

            # declare stop event
            self.stop_event = threading.Event()

            # start read/write threads
            self.t = {}
            for a in ['read', 'write']:
                self.t[a] = threading.Thread(target=getattr(self, a))
                self.t[a].start()

        # login if necessary
        shell_type = self.req_shell_type()
        if  shell_type == 'login':
            self.login()

        return self

    def exec_command(self, cmd, in_data=None, sudoable=True):
        ''' run a command on the remote host'''

        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        # TODO factor these streams initialization
        # open streams for stdout and stderr
        if isinstance(self.stdout, io.BytesIO):
            self.stdout.close()
        if isinstance(self.stderr, io.BytesIO):
            self.stderr.close()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

        display.vvv('>> {0}'.format(repr(cmd)), host=self.host)

        # put stderr in a temporary file and store the return code in a variable
        # TODO use ansible fn to find a suitable place to put it
        stderr_remote = '~{user}/.ansible-serial.stderr'.format(user=self.user)
        cmd = '2>{stderr} {cmd}; CODE=$?'.format(cmd=cmd, stderr=stderr_remote)

        # send the cmd and get stdout
        for m in self.low_cmd(cmd, 'out'):
            self.stdout.write(m)
            display.vvv('<< {0}'.format(m), host=self.host)

        # get return code
        cmd = 'echo "${CODE}"'
        return_code = int(list(self.low_cmd(cmd, 'code'))[0])
        display.vvv('<< {0}'.format(return_code))

        # get stderr and remove temp file
        cmd = 'cat {stderr}; rm {stderr}'.format(stderr=stderr_remote)
        for m in self.low_cmd(cmd, 'err'):
            self.stderr.write(m)
            display.vvv('<< {0}'.format(m), host=self.host)

        # reset cursor on stdout and stderr streams
        self.stdout.seek(0)
        self.stderr.seek(0)

        return (return_code, self.stdout, self.stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        # TODO (in|out)_path sanitization (if not done already)
        display.vvv(u"PUT {0} TO {1}".format(in_path, out_path), host=self.host)

        # cmd for every payload
        #cmd_pre = bytes('head -c -1 >> \'{}\' <<\'<<eof>>\'\n'.format(out_path), 'utf-8')
        #cmd_post = bytes('\n<<eof>>\n', 'utf-8')

        # TODO truly calculate max pyload size

        cmd_pre = bytes('echo -n \'', 'utf-8')
        cmd_post = bytes('\' | base64 -d >> {}\n'.format(out_path), 'utf-8')

        # send start delimiter
        self.q['write'].put(Message('echo "<<--START-TR-->>"\n'))

        with open(in_path, 'rb') as f:
            # split the file in payloads of < 512 bytes
            while (b := f.read(510)):
                # contruct the command and send it
                cmd = Message(cmd_pre + base64.b64encode(b) + cmd_post)
                self.q['write'].put(cmd)

        # send end delimiter
        self.q['write'].put(Message('echo "<<--END-TR-->>"\n'))

        list(self.read_q_until(self.is_line("<<--START-TR-->>"), inclusive=True))
        list(self.read_q_until(self.is_line("<<--END-TR-->>"), inclusive=False))

    def fetch_file(self, in_path, out_path):
        ''' copy a file from remote to local '''

        super(Connection, self).fetch_file(in_path, out_path)

        display.vvv(u'FETCH {0} TO {1}'.format(in_path, out_path), host=self.host)

        # file in reveived in base64
        #best option: (requires coreutils on remote machine)
        #cmd = 'split -b 512 --filter "base64" "{0}"'.format(in_path)
        cmd = 'base64 {0}'.format(in_path)

        # receive the decoded base64 splited file
        with open(out_path, 'wb') as f:
            decode = self.decoder()
            for b in self.low_cmd(cmd, 'fetch'):
                d = decode(b.rstrip())
                f.write(d)

    def close(self):
        display.debug("in close")

        # logout from remote
        self.logout()
        # trigger event to stop the read/write workers
        self.stop_event.set()

        # wait until threads have properly exited
        for a in ['read', 'write']:
            self.t[a].join()

        # close serial connection
        self.ser.close()
        self.is_connected = False

    def read(self):
        ''' read from the serial connection to the read queue '''
        while not self.stop_event.wait(self.loop_interval):
            for received in self.ser:
                display.vvvv('<<<< {0}'.format(repr(received)))
                self.q['read'].put(received)

    def write(self):
        ''' write from the write queue to the serial connection '''
        while not self.stop_event.wait(self.loop_interval):
            if self.q['write'].qsize() > 0:
                qm = self.q['write'].get()

                display.vvvv('>>>> {0}'.format(repr(qm.data)))
                bm = qm.data if type(qm.data) is bytes else bytes(qm.data, 'utf-8')

                # split in smaller payloads
                p_size = self.payload_size
                #payloads = [bm[i:i+p_size] for i in range(0, len(bm), p_size)]
                #for p in payloads:
                self.ser.write(bm)

    def decoder(self):
        ''' b64 decoder with remainder for unbounded messages '''
        rm = b''

        def d(b):
            nonlocal rm
            # append the encoded data to the remainder
            b = rm + b
            # reset the remainder
            rm = b''
            # get the highest length multiple of 4
            rm_len = len(b) % 4
            if rm_len:
                # right side is remaining
                rm = b[-rm_len:]
                # left side is decoded
                b = b[:-rm_len]
            return base64.b64decode(b)
        return d

    def read_q_until(self, break_condition, inclusive=False):
        ''' read the queue until a specified condition '''
        q = self.q['read']
        t_start = time.time()
        timeout = self.read_timeout
        while True:
            if q.qsize() > 0:
                # get the next message
                m = q.get()

                # yield the message and break the loop if needed
                if inclusive: yield m
                if break_condition(m):
                    break
                if not inclusive: yield m

                # after receiving any message, reset the timeout
                t_start = time.time()
            else:
                time.sleep(self.loop_interval)
                if time.time() >= t_start + timeout:
                    raise LookupError(
                        'break_condition "{fn}" has not been met for {t} seconds'.format(
                            fn=repr(break_condition),
                            t=timeout
                    ))

    def is_prompt_line(self, m):
        return m.startswith(self.ps1)

    def is_line(self, line):
        ''' compare a message with a specified line '''
        def c(m):
            if type(m) is bytes:
                try:
                    m = m.decode()
                except(UnicodeDecodeError, AttributeError):
                    return False
            return m.rstrip() == line.rstrip()
        return c

    def is_any_prompt(self, m):
        ''' return true if any type of prompt '''
        return False if self.get_shell_type(m) is None else True

    def low_cmd(self, cmd, delimiter):
        ''' send low-level command '''
        # create delimiters
        s_del = '<<--START-CMD-{0}-->>'.format(delimiter.upper())
        e_del = '<<--END-CMD-{0}-->>'.format(delimiter.upper())

        # encapsulate command
        cmd = 'echo "{s_del}"; {cmd};echo "{e_del}"\n'.format(
                cmd=cmd,
                s_del=s_del,
                e_del=e_del)

        # send commnd to queue
        self.q['write'].put(Message(cmd))

        # flush queue to starting delimiter
        list(self.read_q_until(self.is_line(s_del), inclusive=True))
        
        # yield the output until the ending delimiter
        for m in self.read_q_until(self.is_line(e_del)):
            yield m

    def req_shell_type(self):
        ''' make a request and return the shell type '''
        # send line-feed character
        ctrl_j = chr(10)
        self.q['write'].put(Message(ctrl_j))

        # wait until a prompt is found
        m = list(self.read_q_until(self.is_any_prompt, inclusive=True))[-1]

        # return the shell type
        return self.get_shell_type(m)

    def get_shell_type(self, line):
        ''' return which shell is on the other side '''
        # http://ascii-table.com/ansi-escape-sequences-vt-100.php
        # 7-bit C1 ANSI sequences
        ansi_sequence = re.compile(r'''
            \x1B  # ESC
            (?:   # 7-bit C1 Fe (except CSI)
                [@-Z\\-_]
            |     # or [ for CSI, followed by a control sequence
                \[
                [0-?]*  # Parameter bytes
                [ -/]*  # Intermediate bytes
                [@-~]?   # Final byte
            )
        ''', re.VERBOSE)

        ## end with ANSI CPR (Response to cursor position request)
        #ansi_end_CPR = r'\x1B\[\d+;\d+R$'

        escaped_line = line.decode('unicode_escape')
        # remove ANSI sequences
        clean_line = ansi_sequence.sub('', escaped_line)

        if re.search(' login: $', clean_line):
            return 'login'

        elif re.search('^Password: $', clean_line):
            return 'password'

        elif re.search('(\$|#) $', clean_line):
            self.ps1 = bytes(line.decode().rstrip('\n'), 'utf-8')
            return 'shell'

        else:
            return None

    def login(self):
        self.q['write'].put(Message('{cmd}{end}'.format(cmd=self.user, end='\n')))

        # read the last line
        ll = list(self.read_q_until(self.is_any_prompt, inclusive=True))[-1]
        shell_type = self.get_shell_type(ll)

        if shell_type == 'password':
            self.q['write'].put(Message('{cmd}{end}'.format(cmd=self.passwd, end='\n')))
            #time.sleep(5)
            shell_type = self.req_shell_type()

        if shell_type != 'shell':
            raise AnsibleError('Cannot login')


    def logout(self):
        ctrl_d = chr(4)
        self.q['write'].put(Message(ctrl_d))

        if self.req_shell_type() == 'login':
            display.v('Sucessful logout')
