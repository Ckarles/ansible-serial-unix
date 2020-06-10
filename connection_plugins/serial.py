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

import dataclasses
import io
import queue
import re
import serial
import threading
import time

import ansible.constants as C
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display

display = Display()

@dataclasses.dataclass
class Message:
    '''Message to use in write queue'''
    data: 'typing.Any'
    is_raw: bool = False

class Connection(ConnectionBase):
    ''' Serial based connections '''

    transport = 'serial'
    has_pipelining = False

    # 50ms sleep interval for loops (to no detroy cpu)
    loop_interval = 0.05

    def __init__(self, *args, **kwargs):

        super(Connection, self).__init__(*args, **kwargs)

        user = self._play_context.remote_user
        self.user = user if user else 'root'

        self.host = self._play_context.remote_addr

        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

        self.ser = serial.Serial()

        self.is_connected = False
        self.ps1 = None
        self.q = {a: queue.Queue() for a in ['read', 'write']}

    def __del__(self):
        self.stdout.close()
        self.stderr.close()

    def _connect(self):
        ''' connect to the serial device '''

        if not self.is_connected:
            self.ser.port = self.get_option('serial_port')
            self.payload_size = int(self.get_option('payload_size'))
            self.ser.timeout = 0

            self.ser.open()
            self.is_connected = True

            # stop event
            self.stop_event = threading.Event()
            # start read/write threads
            self.t = {}
            for a in ['read', 'write']:
                self.t[a] = threading.Thread(target=getattr(self, a))
                self.t[a].start()

        if self.req_shell_type() == 'login':
            self.login()

        return self

    def exec_command(self, cmd, in_data=None, sudoable=True):
        ''' run a command on the remote host'''

        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        stderr_remote = '~{user}/.ansible-serial.stderr'.format(user=self.user)

        # log remote command
        display.vvv('>> {0}'.format(repr(cmd)), host=self.host)

        # actual command
        cmd = '2>{stderr} {cmd}; CODE=$?'.format(cmd=cmd, stderr=stderr_remote)

        # send the cmd
        for m in self.low_cmd(cmd, 'out'):
            self.stdout.write(m)
            # log stdout
            display.vvv('<< {0}'.format(m), host=self.host)

        # get return code
        cmd = 'echo "${CODE}"'

        return_code = list(self.low_cmd(cmd, 'code'))[0]

        # get stderr
        cmd = 'cat {stderr}; rm {stderr}'.format(stderr=stderr_remote)

        for m in self.low_cmd(cmd, 'err'):
            self.stderr.write(m)
            # log stderr
            display.vvv('<< {0}'.format(m), host=self.host)

        # reset cursor on stdout and stderr streams
        self.stdout.seek(0)
        self.stderr.seek(0)

        return (int(return_code), self.stdout, self.stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        display.vvv(u"PUT {0} TO {1}".format(in_path, out_path), host=self.host)

        self.q['write'].put(Message('echo "<<--START-TR-->>"\n'))
        with open(in_path, 'rb') as f:
            while (b := f.read(512)):
                self.q['write'].put(Message(bytes('head -c -1 >> \'{}\' <<\'<<eof>>\'\n'.format(out_path), 'utf-8') + b + b'\n<<eof>>\n', is_raw=True))
        self.q['write'].put(Message('echo "<<--END-TR-->>"\n'))

        list(self.read_q_until(self.is_line("<<--START-TR-->>"), inclusive=True))
        list(self.read_q_until(self.is_line("<<--END-TR-->>"), inclusive=False))

    def fetch_file(self, in_path, out_path):
        display.debug("in fetch_file")

    def close(self):
        display.debug("in close")

        self.logout()

        self.stop_event.set()

        for a in ['read', 'write']:
            self.t[a].join()

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
                bm = qm.data if qm.is_raw else bytes(qm.data, 'utf-8')

                p_size = self.payload_size
                # split in smaller payloads
                payloads = [bm[i:i+p_size] for i in range(0, len(bm), p_size)]
                for p in payloads:
                    self.ser.write(p)

    def read_q_until(self, break_condition, inclusive=False):
        ''' read the queue until a specified condition '''
        q = self.q['read']
        # TODO add timeout
        while True:
            if q.qsize() > 0:
                m = q.get()
                if inclusive: yield m
                if break_condition(m):
                    break
                if not inclusive: yield m
            else:
                time.sleep(self.loop_interval)

    def is_prompt_line(self, m):
        return m.startswith(self.ps1)

    def is_line(self, line):
        ''' compare a message with a specified line '''
        def c(m):
            if type(m) is bytes:
                m = m.decode()
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
        
        # yield the output until the endind delimiter
        for m in self.read_q_until(self.is_line(e_del)):
            yield m

    def req_shell_type(self):
        ''' make a request and return the shell type '''
        # send line-feed character
        ctrl_j = chr(10)
        self.q['write'].put(Message(ctrl_j))

        m = list(self.read_q_until(self.is_any_prompt, inclusive=True))[-1]

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

        elif re.search('(\$|#) $', clean_line):
            self.ps1 = bytes(line.decode().rstrip('\n'), 'utf-8')
            return 'shell'

        else:
            return None

    def login(self):
        self.q['write'].put(Message('{cmd}{end}'.format(cmd=self.user, end='\n')))

        if self.req_shell_type() != 'shell':
            display.v('ERROR: cannot login')

    def logout(self):
        ctrl_d = chr(4)
        self.q['write'].put(Message(ctrl_d))

        if self.req_shell_type() == 'login':
            display.v('Sucessful logout')
