# ansible-serial-unix

> Ansible serial connection plugin for unix remote shells

> Non-blocking IO with threads and queues for "acceptable" performances

> Uses pySerial under the hood

## Install
Follow Ansible documentation about [adding a plugin locally](https://docs.ansible.com/ansible/latest/dev_guide/developing_locally.html#adding-a-plugin-locally).
Ansible is not making any distinction between regular files and symbolic links, but git does, so you can just add a symlink to the plugin in the git repo from your chosen *local* ansible plugin dir.

## Requirements
### On the local host:
- python3 (python2 not supported as of now)

### On the remote host:
- Busybox (or equivalent)
- bash (could work on other shells if it supports heredocs, piping and redirection)
- sulogin is the only supported login shell as of now

## Compatibility
I'm not planning on supporting windows remote guests, however the plugin is intended to work from any supported ansible local host.
The plugin has only been tested on a Linux/GNU *posix* host with an Alpine Linux target (*-ash-* & *busybox*). I don't have the time nor the usage to test it on other local and remote systems, but if you intent to use this plugin with different hosts, please tell me if it is working or not on your particular systems so I can fill a compatibility table, and make the plugin compatible with more system condfigurations.

## TODO list
- Find a better way to identify remote's shell
- Support python2 to respect ansible plugin requirements

## Licensing
License is GPLv3 to fit with ansible codebase.
As pySerial is trademarked, I wonder if this piece of code could ever be added to ansible's contributor plugins list, if you know more than me about licensing, I would appreciate if you could help me understand that particular matter.
