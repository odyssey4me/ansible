# Based on local.py (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
# Based on chroot.py (c) 2013, Maykel Moya <mmoya@speedyrails.com>
# (c) 2013, Michael Scherer <misc@zarb.org>
# (c) 2015, Toshio Kuratomi <tkuratomi@ansible.com>
# (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = """
    author: Jesse Pretorius <jesse@odyssey4.me>
    connection: libvirt
    short_description: Run tasks in qemu virtual machines via libvirt
    description:
        - Run commands or put/fetch files to an existing qemu virtual machines using libvirt.
        - Currently DOES NOT work with selinux set to enforcing.
    version_added: "2.10"
    options:
      remote_addr:
        description:
            - Virtual machine identifier
        default: The set user as per docker's configuration
        vars:
            - name: ansible_host
            - name: ansible_libvirt_host
"""

import base64
import json
import libvirt
import libvirt_qemu
import shlex
import traceback

from ansible import constants as C
from ansible.errors import AnsibleError
from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.plugins.connection import ConnectionBase, BUFSIZE
from ansible.utils.display import Display
from functools import partial
from os.path import exists, getsize

display = Display()


class Connection(ConnectionBase):
    ''' Local libvirt qemu based connections '''

    transport = 'libvirt'
    # TODO(odyssey4me):
    # Figure out why pipelining does not work and fix it
    has_pipelining = False
    default_user = 'root'
    has_tty = False

    def __init__(self, play_context, new_stdin, *args, **kwargs):
        super(Connection, self).__init__(play_context, new_stdin, *args, **kwargs)

        self._host = self._play_context.remote_addr
        self._executable = self._play_context.executable

        if self._play_context.remote_user is not None and self._play_context.remote_user != 'root':
            self._display.warning('libvirt does not support remote_user, using virtual machine default: root')

        # TODO(odyssey4me):
        # Figure out how to enable this to connect to a remote URI.
        #   - lib/ansible/modules/cloud/misc/virt.py has inspiration
        # Handle libvirt.libvirtError return from libvirt
        conn = libvirt.open("qemu:///system")
        if not conn:
            raise Exception("hypervisor connection failure")
        self.conn = conn

        domain = conn.lookupByName(self._host)
        if not domain:
            raise Exception("domain connection failure")
        self.domain = domain

    def _connect(self):
        ''' connect to the virtual machine; nothing to do here '''
        super(Connection, self)._connect()
        if not self._connected:
            display.vvv(u"ESTABLISH {0} CONNECTION".format(self.transport), host=self._host)
            self._connected = True

    def exec_command(self, cmd, in_data=None, sudoable=True):
        """ execute a command on the virtual machine host """
        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        self._display.vvv(u"EXEC {0}".format(cmd), host=self._host)

        cmd_args_list = shlex.split(to_native(cmd, errors='surrogate_or_strict'))
        # Remove self._executable from the command list
        # because it is used for the path argument
        del cmd_args_list[0]

        # TODO(odyssey4me):
        # Implement buffering much like the other connection plugins
        # Implement 'env' for the environment settings
        # Implement 'input-data' for whatever it might be useful for
        request_exec = {
            'execute': 'guest-exec',
            'arguments': {
                'path': self._executable,
                'capture-output': True,
                'arg': cmd_args_list
            }
        }
        request_exec_json = json.dumps(request_exec)

        display.vvv(u"GA send: {0}".format(request_exec_json), host=self._host)

        # TODO(odyssey4me):
        # Add timeout parameter
        result_exec = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_exec_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_exec), host=self._host)

        request_status = {
            'execute': 'guest-exec-status',
            'arguments': {
                'pid': result_exec['return']['pid']
            }
        }
        request_status_json = json.dumps(request_status)

        display.vvv(u"GA send: {0}".format(request_status_json), host=self._host)

        # TODO(odyssey4me):
        # Work out a better way to wait until the command has exited
        result_status = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_status_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_status), host=self._host)

        while not result_status['return']['exited']:
            result_status = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_status_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_status), host=self._host)

        if result_status['return'].get('out-data'):
            stdout = to_text(base64.b64decode(result_status['return']['out-data']))
        else:
            stdout = ''

        if result_status['return'].get('err-data'):
            stderr = to_text(base64.b64decode(result_status['return']['err-data']))
        else:
            stderr = ''

        display.vvv(u"GA stdout: {0}".format(stdout), host=self._host)
        display.vvv(u"GA stderr: {0}".format(stderr), host=self._host)

        return result_status['return']['exitcode'], stdout, stderr

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to domain '''
        super(Connection, self).put_file(in_path, out_path)
        display.vvv("PUT %s TO %s" % (in_path, out_path), host=self._host)

        if not exists(to_bytes(in_path, errors='surrogate_or_strict')):
            raise AnsibleFileNotFound(
                "file or module does not exist: %s" % in_path)

        request_handle = {
            'execute': 'guest-file-open',
            'arguments': {
                'path': out_path,
                'mode': 'wb+'
            }
        }
        request_handle_json = json.dumps(request_handle)

        display.vvv(u"GA send: {0}".format(request_handle_json), host=self._host)

        result_handle = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_handle_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_handle), host=self._host)

        # TODO(odyssey4me):
        # Handle exception for file/path IOError
        with open(to_bytes(in_path, errors='surrogate_or_strict'), 'rb') as in_file:
            for chunk in iter(partial(in_file.read, BUFSIZE), b''):
                try:
                    request_write = {
                        'execute': 'guest-file-write',
                        'arguments': {
                            'handle': result_handle['return'],
                            'buf-b64': base64.b64encode(chunk).decode()
                        }
                    }
                    request_write_json = json.dumps(request_write)

                    display.vvvvv(u"GA send: {0}".format(request_write_json), host=self._host)

                    result_write = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_write_json, 5, 0))

                    display.vvvvv(u"GA return: {0}".format(result_write), host=self._host)

                except Exception:
                    traceback.print_exc()
                    raise AnsibleError("failed to transfer file %s to %s" % (in_path, out_path))

        request_close = {
            'execute': 'guest-file-close',
            'arguments': {
                'handle': result_handle['return']
            }
        }
        request_close_json = json.dumps(request_close)

        display.vvv(u"GA send: {0}".format(request_close_json), host=self._host)

        result_close = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_close_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_close), host=self._host)

    def fetch_file(self, in_path, out_path):
        ''' fetch a file from domain to local '''
        super(Connection, self).fetch_file(in_path, out_path)
        display.vvv("FETCH %s TO %s" % (in_path, out_path), host=self._host)

        request_handle = {
            'execute': 'guest-file-open',
            'arguments': {
                'path': in_path,
                'mode': 'r'
            }
        }
        request_handle_json = json.dumps(request_handle)

        display.vvv(u"GA send: {0}".format(request_handle_json), host=self._host)

        result_handle = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_handle_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_handle), host=self._host)

        request_read = {
            'execute': 'guest-file-read',
            'arguments': {
                'handle': result_handle['return'],
                'count': BUFSIZE
            }
        }
        request_read_json = json.dumps(request_read)

        display.vvv(u"GA send: {0}".format(request_read_json), host=self._host)

        with open(to_bytes(out_path, errors='surrogate_or_strict'), 'wb+') as out_file:
            try:
                result_read = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_read_json, 5, 0))
                display.vvvvv(u"GA return: {0}".format(result_read), host=self._host)
                out_file.write(base64.b64decode(result_read['return']['buf-b64']))
                while not result_read['return']['eof']:
                    result_read = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_read_json, 5, 0))
                    display.vvvvv(u"GA return: {0}".format(result_read), host=self._host)
                    out_file.write(base64.b64decode(result_read['return']['buf-b64']))

            except Exception:
                traceback.print_exc()
                raise AnsibleError("failed to transfer file %s to %s" % (in_path, out_path))

        request_close = {
            'execute': 'guest-file-close',
            'arguments': {
                'handle': result_handle['return']
            }
        }
        request_close_json = json.dumps(request_close)

        display.vvv(u"GA send: {0}".format(request_close_json), host=self._host)

        result_close = json.loads(libvirt_qemu.qemuAgentCommand(self.domain, request_close_json, 5, 0))

        display.vvv(u"GA return: {0}".format(result_close), host=self._host)

    def close(self):
        ''' terminate the connection; nothing to do here '''
        super(Connection, self).close()
        self._connected = False
