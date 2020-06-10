# ansible-serial-unix

> Serial connection plugin for unix remote shells

> Non-blocking IO with threads and queues for "acceptable" performances

> Uses pySerial under the hood

## Install
Follow Ansible documentation about [adding a plugin locally](https://docs.ansible.com/ansible/latest/dev_guide/developing_locally.html#adding-a-plugin-locally).
Ansible is not making any distinction between regular files and symbolic links, but git does, so you can just add a symlink to the plugin in the git repo from your chosen *local* ansible plugin dir.

## Requirements
python3 on the local host, any unix shell on the target.

## Compatibility
I'm not planning on supporting windows remote guests, however the plugin is intended to work from an ansible local windows host.
The plugin has only been tested on a Linux/GNU *posix* host with a *-ash* Linux target. I don't have the time nor the usage to test it on other local and remote systems, but if you are using this plugin with different hosts, please tell me if it works or not on your particular systems so I can fill a compatibility table, and make the plugin compatible with more system condfigurations.

## Licensing
License is GPLv3 to fit with ansible codebase.
As pySerial is trademarked, I wonder if this piece of code could ever be added to ansible's contributor plugins list, if you know more than me about licensing, I would appreciate if you could tell me more on that particular matter.
