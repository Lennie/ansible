# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

#############################################

import ansible.inventory
import ansible.runner
import ansible.constants as C
from ansible import utils
from ansible import errors
import os
from play import Play

#############################################

class PlayBook(object):

    '''
    runs an ansible playbook, given as a datastructure
    or YAML filename.  a playbook is a deployment, config
    management, or automation based set of commands to
    run in series.

    multiple plays/tasks do not execute simultaneously,
    but tasks in each pattern do execute in parallel
    (according to the number of forks requested) among
    the hosts they address
    '''

    # *****************************************************

    def __init__(self,
        playbook         = None,
        host_list        = C.DEFAULT_HOST_LIST,
        module_path      = C.DEFAULT_MODULE_PATH,
        forks            = C.DEFAULT_FORKS,
        timeout          = C.DEFAULT_TIMEOUT,
        remote_user      = C.DEFAULT_REMOTE_USER,
        remote_pass      = C.DEFAULT_REMOTE_PASS,
        sudo_pass        = C.DEFAULT_SUDO_PASS,
        remote_port      = C.DEFAULT_REMOTE_PORT,
        transport        = C.DEFAULT_TRANSPORT,
        private_key_file = C.DEFAULT_PRIVATE_KEY_FILE,
        debug            = False,
        callbacks        = None,
        runner_callbacks = None,
        stats            = None,
        sudo             = False,
        sudo_user        = C.DEFAULT_SUDO_USER,
        extra_vars       = None):

        """
        playbook:         path to a playbook file
        host_list:        path to a file like /etc/ansible/hosts
        module_path:      path to ansible modules, like /usr/share/ansible/
        forks:            desired level of paralellism
        timeout:          connection timeout
        remote_user:      run as this user if not specified in a particular play
        remote_pass:      use this remote password (for all plays) vs using SSH keys
        sudo_pass:        if sudo==True, and a password is required, this is the sudo password
        remote_port:      default remote port to use if not specified with the host or play
        transport:        how to connect to hosts that don't specify a transport (local, paramiko, etc)
        callbacks         output callbacks for the playbook
        runner_callbacks: more callbacks, this time for the runner API
        stats:            holds aggregrate data about events occuring to each host
        sudo:             if not specified per play, requests all plays use sudo mode
        """

        self.SETUP_CACHE = {}

        if playbook is None or callbacks is None or runner_callbacks is None or stats is None:
            raise Exception('missing required arguments')

        if extra_vars is None:
            extra_vars = {}
       
        self.module_path      = module_path
        self.forks            = forks
        self.timeout          = timeout
        self.remote_user      = remote_user
        self.remote_pass      = remote_pass
        self.remote_port      = remote_port
        self.transport        = transport
        self.debug            = debug
        self.callbacks        = callbacks
        self.runner_callbacks = runner_callbacks
        self.stats            = stats
        self.sudo             = sudo
        self.sudo_pass        = sudo_pass
        self.sudo_user        = sudo_user
        self.extra_vars       = extra_vars
        self.global_vars      = {}
        self.private_key_file = private_key_file

        self.inventory = ansible.inventory.Inventory(host_list)
        
        if not self.inventory._is_script:
            self.global_vars.update(self.inventory.get_group_variables('all'))

        self.basedir    = os.path.dirname(playbook)
        self.playbook  = utils.parse_yaml_from_file(playbook)

        self.module_path = self.module_path + os.pathsep + os.path.join(self.basedir, "library")

    # *****************************************************
        
    def run(self):
        ''' run all patterns in the playbook '''

        # loop through all patterns and run them
        self.callbacks.on_start()
        for play_ds in self.playbook:
            self.SETUP_CACHE = {}
            self._run_play(Play(self,play_ds))

        # summarize the results
        results = {}
        for host in self.stats.processed.keys():
            results[host] = self.stats.summarize(host)
        return results

    # *****************************************************

    def _async_poll(self, poller, async_seconds, async_poll_interval):
        ''' launch an async job, if poll_interval is set, wait for completion '''

        results = poller.wait(async_seconds, async_poll_interval)

        # mark any hosts that are still listed as started as failed
        # since these likely got killed by async_wrapper
        for host in poller.hosts_to_poll:
            reason = { 'failed' : 1, 'rc' : None, 'msg' : 'timed out' }
            self.runner_callbacks.on_failed(host, reason)
            results['contacted'][host] = reason

        return results

    # *****************************************************

    def _run_task_internal(self, task):
        ''' run a particular module step in a playbook '''

        hosts = [ h for h in self.inventory.list_hosts() if (h not in self.stats.failures) and (h not in self.stats.dark)]
        self.inventory.restrict_to(hosts)

        runner = ansible.runner.Runner(
            pattern=task.play.hosts, inventory=self.inventory, module_name=task.module_name,
            module_args=task.module_args, forks=self.forks,
            remote_pass=self.remote_pass, module_path=self.module_path,
            timeout=self.timeout, remote_user=task.play.remote_user, 
            remote_port=task.play.remote_port, module_vars=task.module_vars,
            private_key_file=self.private_key_file,
            setup_cache=self.SETUP_CACHE, basedir=self.basedir,
            conditional=task.only_if, callbacks=self.runner_callbacks, 
            debug=self.debug, sudo=task.play.sudo, sudo_user=task.play.sudo_user,
            transport=task.play.transport, sudo_pass=self.sudo_pass, is_playbook=True
        )

        if task.async_seconds == 0:
            results = runner.run()
        else:
            results, poller = runner.run_async(task.async_seconds)
            self.stats.compute(results)
            if task.async_poll_interval > 0:
                # if not polling, playbook requested fire and forget, so don't poll
                results = self._async_poll(poller, task.async_seconds, task.async_poll_interval)

        self.inventory.lift_restriction()
        return results

    # *****************************************************

    def _run_task(self, play, task, is_handler):
        ''' run a single task in the playbook and recursively run any subtasks.  '''

        self.callbacks.on_task_start(task.name, is_handler)

        # load up an appropriate ansible runner to run the task in parallel
        results = self._run_task_internal(task)

        # add facts to the global setup cache
        for host, result in results['contacted'].iteritems():
            if "ansible_facts" in result:
                for k,v in result['ansible_facts'].iteritems():
                    self.SETUP_CACHE[host][k]=v

        self.stats.compute(results)

        # if no hosts are matched, carry on
        if results is None:
            results = {}
 
        # flag which notify handlers need to be run
        if len(task.notify) > 0:
            for host, results in results.get('contacted',{}).iteritems():
                if results.get('changed', False):
                    for handler_name in task.notify:
                        self._flag_handler(play.handlers(), utils.template(handler_name, task.module_vars), host)

    # *****************************************************

    def _flag_handler(self, handlers, handler_name, host):
        ''' 
        if a task has any notify elements, flag handlers for run
        at end of execution cycle for hosts that have indicated
        changes have been made
        '''

        found = False
        for x in handlers:
            if handler_name == x.name:
                found = True
                self.callbacks.on_notify(host, x.name)
                x.notified_by.append(host)
        if not found:
            raise errors.AnsibleError("change handler (%s) is not defined" % handler_name)

    # *****************************************************

    def _do_setup_step(self, play, vars_files=None):

        ''' push variables down to the systems and get variables+facts back up '''

        # this enables conditional includes like $facter_os.yml and is only done
        # after the original pass when we have that data.
        #

        if vars_files is not None:
            self.callbacks.on_setup_secondary()
            play.update_vars_files(self.inventory.list_hosts(play.hosts))
        else:
            self.callbacks.on_setup_primary()

        host_list = [ h for h in self.inventory.list_hosts(play.hosts) 
            if not (h in self.stats.failures or h in self.stats.dark) ]

        self.inventory.restrict_to(host_list)

        # push any variables down to the system
        setup_results = ansible.runner.Runner(
            pattern=play.hosts, module_name='setup', module_args=play.vars, inventory=self.inventory,
            forks=self.forks, module_path=self.module_path, timeout=self.timeout, remote_user=play.remote_user,
            remote_pass=self.remote_pass, remote_port=play.remote_port, private_key_file=self.private_key_file,
            setup_cache=self.SETUP_CACHE, callbacks=self.runner_callbacks, sudo=play.sudo, sudo_user=play.sudo_user, 
            debug=self.debug, transport=play.transport, sudo_pass=self.sudo_pass, is_playbook=True
        ).run()
        self.stats.compute(setup_results, setup=True)

        self.inventory.lift_restriction()

        # now for each result, load into the setup cache so we can
        # let runner template out future commands
        setup_ok = setup_results.get('contacted', {})
        if vars_files is None:
            # first pass only or we'll erase good work
            for (host, result) in setup_ok.iteritems():
                if 'ansible_facts' in result:
                    self.SETUP_CACHE[host] = result['ansible_facts']
        return setup_results

    # *****************************************************

    def _run_play(self, play):
        ''' run a list of tasks for a given pattern, in order '''

        self.callbacks.on_play_start(play.name)

        # push any variables down to the system # and get facts/ohai/other data back up
        rc = self._do_setup_step(play) # pattern, vars, user, port, sudo, sudo_user, transport, None)

        # now with that data, handle contentional variable file imports!
        if play.vars_files and len(play.vars_files) > 0:
            rc = self._do_setup_step(play, play.vars_files)

        # run all the top level tasks, these get run on every node
        for task in play.tasks():
            self._run_task(play, task, False)

        # run notify actions
        for handler in play.handlers():
            if len(handler.notified_by) > 0:
                self.inventory.restrict_to(handler.notified_by)
                self._run_task(play, handler, True)
                self.inventory.lift_restriction()

