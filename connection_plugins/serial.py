from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    connection: serial
    short_description: execute on a serial device
    description:
        - This connection plugin allows ansible to execute tasks over a serial device.
    author: Charles Durieux
    version_added: historical
    notes:
        - foobar
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

        #self.user = self._play_context.remote_user
        self.user = 'root'
        self.ser = serial.Serial()
        self.ser.port = '/dev/pts/2'
        self.ser.timeout = 1

        self.is_connected = False
        self.rw_queue = queue.SimpleQueue()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        
    def __del__(self):
        self.stdout.close()
        self.stderr.close()

    def _connect(self):
        ''' connect to the serial device '''

        if not self.is_connected:
            self.ser.open()
            self.is_connected = True

        if self.get_shell_type() == 'login':
            self.login()

        return self

    def exec_command(self, cmd, in_data=None, sudoable=True, end='\n'):
        ''' run a command on the local host '''

        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        display.vv('>> {0}'.format(cmd))
        self.ser.write('{cmd}{end}'.format(cmd=cmd, end=end).encode())

        for l in self.read_buffer():
            #display.vv('<< {0}'.format(repr(l)))
            m = l
            if l.startswith(self.ps1):
                break
            #m = l.replace(self.ps1, '', 1)
            self.stdout.write(m)
            #display.vv('<< {0}'.format(m))

        self.ser.write(b'echo $?\n')
        code = list(self.read_buffer())[1]

        return (int(code), self.stdout, self.stderr)

    def put_file(self, in_path, out_path):
        display.debug("in put_file")
        with open(in_path, 'rb') as f:
            self.ser.write('cat << eof > {}'.format(out_path).encode())

            for b in f:
                self.ser.write(b)

            self.ser.write('eof'.encode())

    def fetch_file(self, in_path, out_path):
        display.debug("in fetch_file")

    def close(self):
        display.debug("in close")

        self.logout()
        self.ser.close()

    def read_buffer(self, raw=False):
        # 7-bit C1 ANSI sequences
        ansi_escape = re.compile(r'''
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

        for b in self.ser:

            # flush the queue first
            if not self.rw_queue.empty():
                if self.rw_queue.get() != b:
                    # raise error
                    display.v('error: returned message is distorded')

            if raw:
                yield b
            #display.vv(b.decode())
            # decode the line
            d = b.decode('unicode_escape')
            # get rid of escape sequences
            e = ansi_escape.sub('', d)
            display.vvv('<< {0}'.format(e))
            yield e

    def get_shell_type(self):
        # send line-feed character
        ctrl_j = chr(10)
        self.ser.write(bytes(ctrl_j, 'utf-8'))

        line = ''

        for l in self.read_buffer():
            line = l

        if re.search(' login: $', line):
            display.debug('login ready')
            return 'login'

        elif re.search('(\$|#) $', line):
            self.ps1 = line
            display.debug('shell ready')
            return 'shell'

        else:
            print('ERROR: unkown state')
            return 'unkown'


    def login(self):
        self.ser.write('{cmd}{end}'.format(cmd=self.user, end='\n').encode())

        if self.get_shell_type() != 'shell':
            print('ERROR: cannot login')

    def logout(self):
        ctrl_d = chr(4)
        self.ser.write(bytes(ctrl_d, 'utf-8'))

        if self.get_shell_type() == 'login':
            display.debug('Sucessful logout')

    def write_buffer(m):
        self.rw_queue.put(m)
        self.ser.write(m)
