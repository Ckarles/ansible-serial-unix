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
      remote_user:
        description:
          - User name with which to login to the remote server, normally set by the remote_user keyword.
          - If no user is supplied, root is used
        default: root
        ini:
          - section: defaults
            key: remote_user
        env:
          - name: ANSIBLE_REMOTE_USER
        vars:
          - name: ansible_user
      serial_timeout:
        description:
          - Number of seconds the connection will wait for a response
        default: 1
        type: integer
        ini:
          - section: defaults
            key: timeout
        env:
          - name: ANSIBLE_SERIAL_TIMEOUT
        vars:
          - name: ansible_serial_timeout
'''

import io
import serial
import re
import queue

import ansible.constants as C
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display

display = Display()

class Connection(ConnectionBase):
    ''' Serial based connections '''

    transport = 'serial'
    has_pipelining = False

    def __init__(self, *args, **kwargs):

        super(Connection, self).__init__(*args, **kwargs)

        user = self._play_context.remote_user
        self.user = user if user else 'root'

        host = self._play_context.remote_addr
        self.host = host if host else '/dev/ttyS0'

        self.ser = serial.Serial()

        self.is_connected = False
        self.rw_queue = queue.SimpleQueue()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        
    def __del__(self):
        self.stdout.close()
        self.stderr.close()

    def _connect(self):
        ''' connect to the serial device '''

        if not self.is_connected:
            self.ser.port = self.get_option('serial_port')
            self.ser.timeout = self.get_option('serial_timeout')

            self.ser.open()
            self.is_connected = True
            self.ser_text = io.TextIOWrapper(self.ser)

        if self.get_shell_type() == 'login':
            self.login()

        return self

    def exec_command(self, cmd, in_data=None, sudoable=True, end='\n'):
        ''' run a command on the remote host'''

        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        stderr_remote = 'error-serial.an'

        # Append return code request to the command
        display.vvv('>> {0}'.format(repr(cmd)), host=self.host)
        cmd = '{cmd} 2>{stderr}; echo $?{end}'.format(cmd=cmd, end=end, stderr=stderr_remote)
        self.write_buffer(cmd)

        # read the output of the command, store the last line in the code var only
        m = None
        for l in self.read_buffer():
            # stop reading when getting a command prompt
            if l.startswith(self.ps1):
                break
            if m:
                self.stdout.write(bytes(m, 'utf-8'))
                display.vvv('<< {0}'.format(m), host=self.host)
            m = l
        code = m
        # reset cursor on stdout stream
        self.stdout.seek(0)

        # get stderr
        self.write_buffer('cat {stderr}; rm {stderr}'.format(stderr=stderr_remote))
        for l in self.read_buffer():
            # stop reading when getting a command prompt
            if l.startswith(self.ps1):
                break
            self.stderr.write(bytes(m, 'utf-8'))
            display.vvv('<< {0}'.format(m), host=self.host)

        self.stderr.seek(0)

        return (int(code), self.stdout, self.stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        display.vvv(u"PUT {0} TO {1}".format(in_path, out_path), host=self.host)

        with open(in_path, 'rb') as f:
            while (b := f.read(512)):
                self.write_buffer(bytes('head -c -1 >> \'{}\' <<\'<<eof>>\'\n'.format(out_path), 'utf-8') + b + b'\n<<eof>>\n', raw=True, echo=False)

        # flush read buffer
        list(self.ser)

    def fetch_file(self, in_path, out_path):
        display.debug("in fetch_file")

    def close(self):
        display.debug("in close")

        self.logout()
        self.ser_text.close()
        self.rw_queue.close()
        self.ser.close()
        self.is_connected = False


    def read_buffer(self, raw=False):

        stream = self.ser_text

        overtext = ''

        for m in stream:
            #display.vvv('<<<< {0}'.format(repr(m)))
            #display.vvv('---- overtext: {0}'.format(repr(overtext)))
            if not overtext:
                if self.rw_queue.empty():
                    #display.vvv('---- yield: {0}'.format(repr(m)))
                    yield m
                    continue
                else:
                    qm = self.rw_queue.get()
            else:
                qm = overtext

            if qm == m:
                overtext = ''
                continue
            else:
                m = m.rstrip('\n')
                if qm.startswith(m):
                    overtext = qm.replace(m, '', 1)
                else:
                    # raise error
                    display.v('error: echo seems distorded: \n expected: {0}\n received: {1}'.format(repr(qm), repr(m)))


    def get_shell_type(self, line=None):

        # get a prompt invite none
        if not line:
            # send line-feed character
            ctrl_j = chr(10)
            self.write_buffer(ctrl_j, echo=False)

            line = list(self.read_buffer())[-1]

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

        escaped_line = bytes(line, 'utf-8').decode('unicode_escape')
        # remove ANSI sequences
        clean_line = ansi_sequence.sub('', escaped_line)

        if re.search(' login: $', clean_line):
            return 'login'

        elif re.search('(\$|#) $', clean_line):
            self.ps1 = line
            return 'shell'

        else:
            return 'unkown'

    def login(self):
        self.write_buffer('{cmd}{end}'.format(cmd=self.user, end='\n'))

        line = list(self.read_buffer())[-1]

        if self.get_shell_type(line=line) != 'shell':
            print('ERROR: cannot login')

    def logout(self):
        ctrl_d = chr(4)
        self.write_buffer(ctrl_d, echo=False)

        line = list(self.read_buffer())[-1]

        if self.get_shell_type(line=line) == 'login':
            display.debug('Sucessful logout')

    def write_buffer(self, m, raw=False, echo=True):
        if echo:
            self.rw_queue.put(m)
        if raw:
            self.ser.write(m)
        else:
            self.ser_text.write(m)
