#!/usr/bin/env python
# encoding: utf-8

import yaml
import threading
from zstacklib import *
from utils import linux
from utils import shell
from utils.sql_query import MySqlCommandLineQuery
from termcolor import colored
from datetime import datetime, timedelta


def info_verbose(*msg):
    if len(msg) == 1:
        out = '%s\n' % ''.join(msg)
    else:
        out = ''.join(msg)
    now = datetime.now()
    out = "%s " % str(now) + out
    sys.stdout.write(out)
    logger.info(out)


def collect_fail_verbose(*msg):
    if len(msg) == 1:
        out = '%s\n' % ''.join(msg)
    else:
        out = ''.join(msg)
    now = datetime.now()
    out = "%s " % str(now) + out
    return out


def error_verbose(msg):
    sys.stderr.write(colored('ERROR: %s\n' % msg, 'red'))
    sys.exit(1)


class CtlError(Exception):
    pass


def warn(msg):
    logger.warn(msg)
    sys.stdout.write(colored('WARNING: %s\n' % msg, 'yellow'))


def get_default_ip():
    cmd = shell.ShellCmd(
        """dev=`ip route|grep default|head -n 1|awk -F "dev" '{print $2}' | awk -F " " '{print $1}'`; ip addr show $dev |grep "inet "|awk '{print $2}'|head -n 1 |awk -F '/' '{print $1}'""")
    cmd(False)
    return cmd.stdout.strip()

class CollectTime(object):
    def __init__(self, start_time, end_time, total_collect_time):
        self.start_time = start_time.strftime("%Y-%m-%d %H:%M:%S")
        self.end_time = end_time.strftime("%Y-%m-%d %H:%M:%S")
        self.total_collect_time = total_collect_time

class FailDetail(object):
    def __init__(self, fail_log_name, fail_cause):
        self.fail_log_name = fail_log_name
        self.fail_cause = fail_cause

class Summary(object):
    def __init__(self):
        self.fail_count = 0
        self.success_count = 0
        self.collect_time_list = {}
        self.fail_list = {}

    def add_fail(self, log_type, ip, fail_detail):
        ip_dict = self.fail_list.get(log_type)
        if ip_dict is None:
            self.fail_list[log_type] = {ip: [fail_detail]}
            return

        detail_list = ip_dict.get(ip)
        if detail_list is None:
            ip_dict[ip] = [fail_detail]
            return

        detail_list.append(fail_detail)

    def add_collect_time(self, log_type, ip, collect_time):
        ip_dict = self.collect_time_list.get(log_type)
        if ip_dict is None:
            self.collect_time_list[log_type] = {ip: [collect_time]}
            return

        time_list = ip_dict.get(ip)
        if time_list is None:
            ip_dict[ip] = [collect_time]
            return

        time_list.append(collect_time)

    def persist(self, collect_dir):
        summary_file = collect_dir + 'summary'
        with open(summary_file, 'a+') as f:
            f.write(json.dumps({"fail_count": self.fail_count,
                                "success_count": self.success_count,
                                "fail_list": self.fail_list,
                                "collect_time_list": self.collect_time_list}, default=lambda o: o.__dict__,
                               indent=4))

class CollectFromYml(object):
    failed_flag = False
    f_date = None
    t_date = None
    since = None
    logger_dir = '/var/log/zstack/'
    logger_file = 'zstack-ctl.log'
    vrouter_tmp_log_path = '/home'
    threads = []
    local_type = 'local'
    host_type = 'host'
    check_lock = threading.Lock()
    suc_lock = threading.Lock()
    fail_lock = threading.Lock()
    ha_conf_dir = "/var/lib/zstack/ha/"
    ha_conf_file = ha_conf_dir + "ha.yaml"
    check = False
    check_result = {}
    max_thread_num = 20
    DEFAULT_ZSTACK_HOME = '/usr/local/zstack/apache-tomcat/webapps/zstack/'
    HA_KEEPALIVED_CONF = "/etc/keepalived/keepalived.conf"
    summary = Summary()

    def __init__(self, ctl, collect_dir, detail_version, time_stamp, args):
        self.ctl = ctl
        self.run(collect_dir, detail_version, time_stamp, args)

    def get_host_sql(self, suffix_sql):
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        if db_password:
            cmd = "mysql --host %s --port %s -u%s -p%s zstack -e \'%s\'" % (
                db_hostname, db_port, db_user, db_password, suffix_sql)
        else:
            cmd = "mysql --host %s --port %s -u%s zstack -e \'%s\'" % (db_hostname, db_port, db_user, suffix_sql)
        return cmd

    def get_dump_sql(self):
        mysqldump_skip_tables = "--ignore-table=zstack.VmUsageHistoryVO --ignore-table=zstack.RootVolumeUsageHistoryVO " \
                        "--ignore-table=zstack.NotificationVO --ignore-table=zstack.PubIpVmNicBandwidthUsageHistoryVO " \
                        "--ignore-table=zstack.DataVolumeUsageHistoryVO " \
                        "--ignore-table=zstack.ResourceUsageVO --ignore-table=zstack.PciDeviceUsageHistoryVO " \
                        "--ignore-table=zstack.PubIpVipBandwidthUsageHistoryVO"
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        if db_password:
            cmd = "mysqldump --database -u%s -p%s -P %s --single-transaction --quick zstack zstack_rest information_schema performance_schema %s" % (
                db_user, db_password, db_port, mysqldump_skip_tables)
        else:
            cmd = "mysqldump --database -u%s -P %s --single-transaction --quick zstack zstack_rest information_schema performance_schema %s" % (
                db_user, db_port, mysqldump_skip_tables)
        return cmd

    def decode_conf_yml(self, args):
        base_conf_path = '/var/lib/zstack/virtualenv/zstackctl/lib/python2.7/site-packages/zstackctl/conf/'
        default_yml_mn_only = 'collect_log_mn_only.yaml'
        default_yml_mn_db = 'collect_log_mn_db.yaml'
        default_yml_full = 'collect_log_full.yaml'
        default_yml_full_db = 'collect_log_full_db.yaml'
        default_yml_mn_host = "collect_log_mn_host.yaml"
        yml_conf_dir = None
        name_array = []

        if args.mn_only:
            yml_conf_dir = base_conf_path + default_yml_mn_only
        elif args.mn_db:
            yml_conf_dir = base_conf_path + default_yml_mn_db
        elif args.full:
            yml_conf_dir = base_conf_path + default_yml_full
        elif args.full_db:
            yml_conf_dir = base_conf_path + default_yml_full_db
        elif args.mn_host:
            yml_conf_dir = base_conf_path + default_yml_mn_host
        else:
            if args.p is None:
                yml_conf_dir = base_conf_path + default_yml_full
            else:
                yml_conf_dir = args.p

        decode_result = {}
        decode_error = None
        if not os.path.exists(yml_conf_dir):
            decode_error = 'do not find conf path %s' % yml_conf_dir
            decode_result['decode_error'] = decode_error
            return decode_result
        f = open(yml_conf_dir)
        try:
            conf_dict = yaml.load(f)
        except:
            decode_error = 'decode yml error,please check the yml'
            decode_result['decode_error'] = decode_error
            return decode_result

        for conf_key, conf_value in conf_dict.items():
            collect_type = conf_key
            list_value = conf_value.get('list')
            logs = conf_value.get('logs')
            if list_value is None or logs is None:
                decode_error = 'host or log can not be empty in %s' % log
                break

            if 'exec' not in list_value:
                if '\n' in list_value:
                    temp_array = list_value.split('\n')
                    conf_value['list'] = temp_array
                elif ',' in list_value:
                    temp_array = list_value.split(',')
                    conf_value['list'] = temp_array
                else:
                    if ' ' in list_value:
                        temp_array = list_value.split()
                        conf_value['list'] = temp_array

            if collect_type == 'host' or collect_type == 'sharedblock':
                if args.hosts is not None:
                    conf_value['list'] = args.hosts.split(',')

            history_configured = False

            for log in logs:
                name_value = log.get('name')
                dir_value = log.get('dir')
                file_value = log.get('file')
                exec_value = log.get('exec')
                mode_value = log.get('mode')
                exec_type_value = log.get('exec_type')
                if name_value is None:
                    decode_error = 'log name can not be None in %s' % log
                    break
                else:
                    if name_value in name_array:
                        decode_error = 'duplicate name key :%s' % name_value
                        break
                    else:
                        name_array.append(name_value)

                    if name_value == 'history':
                        history_configured = True
                if dir_value is None:
                    if exec_value is None:
                        decode_error = 'dir, exec cannot be empty at the same time in  %s' % log
                        break
                    if name_value == 'mysql-database' and exec_value == 'AutoCollect':
                        log['exec'] = self.get_dump_sql()
                    if exec_type_value is None:
                        log['exec_type'] = 'RunAndRedirect'
                else:
                    if str(dir_value).startswith('$ZSTACK_HOME'):
                        dir_value = str(dir_value).replace('$ZSTACK_HOME', self.DEFAULT_ZSTACK_HOME)
                        log['dir'] = dir_value
                    if str(dir_value).startswith('/') is not True:
                        decode_error = 'dir must be an absolute path in %s' % log
                        break
                    if file_value is not None and file_value.startswith('/'):
                        decode_error = 'file value can not be an absolute path in %s' % log
                        break
                if mode_value is None:
                    log['mode'] = "Normal"

            # collect `history` by default
            if not history_configured:
                logs.append({'name': 'history', 'mode': 'Normal', 'dir': '/var/log/history.d/', 'file': 'history'})

            decode_result[collect_type] = dict(
                (key, value) for key, value in conf_value.items() if key == 'list' or key == 'logs')
            name_array = []

        decode_result['decode_error'] = decode_error
        return decode_result

    def build_collect_cmd(self, log, collect_dir):
        dir_value = log['dir']
        file_value = log['file']
        mode_value = log['mode']
        cmd = 'find %s -type f' % dir_value
        if file_value is not None:
            if file_value.startswith('regex='):
                cmd = cmd + ' -regex \'%s\'' % file_value
            else:
                cmd = cmd + ' -name \'%s\'' % file_value

        if mode_value == "Normal":
            cmd = cmd + ' -exec ls --full-time {} \; | sort -k6 | awk \'{print $6\":\"$7\"|\"$9\"|\"$5}\''
            cmd = cmd + ' | awk -F \'|\' \'BEGIN{preview=0;} {if(NR==1 && ( $1 > \"%s\" || (\"%s\" < $1 && $1  <= \"%s\"))) print $2\"|\"$3; \
                                   else if ((\"%s\" < $1 && $1 <= \"%s\") || ( $1> \"%s\" && preview < \"%s\")) print $2\"|\"$3; preview = $1}\'' \
                  % (self.t_date, self.f_date, self.t_date, self.f_date, self.t_date, self.t_date, self.t_date)
        if self.check:
            cmd = cmd + '| awk -F \'|\' \'BEGIN{size=0;} \
                   {size = size + $2/1024/1024;}  END{size=sprintf("%.1f", size); print size\"M\";}\''
        else:
            cmd = cmd + ' | awk -F \'|\' \'{print $1}\'| xargs -I {} /bin/cp -rpf {} %s' % collect_dir
        return cmd

    def build_collect_cmd_old(self, dir_value, file_value, collect_dir):
        cmd = 'find %s' % dir_value
        if file_value is not None:
            if file_value.startswith('regex='):
                cmd = cmd + ' -regex \'%s\'' % file_value
            else:
                cmd = cmd + ' -name \'%s\'' % file_value
        if self.since is not None:
            cmd = cmd + ' -mtime -%s' % self.since
        if self.f_date is not None:
            cmd = cmd + ' -newermt \'%s\'' % self.f_date
        if self.t_date is not None:
            cmd = cmd + ' ! -newermt \'%s\'' % self.t_date
        if self.check:
            cmd = cmd + ' -exec ls -l {} \; | awk \'BEGIN{size=0;} \
                {size = size + $5/1024/1024;}  END{size=sprintf("%.1f", size); print size\"M\";}\''
        else:
            cmd = cmd + ' -exec /bin/cp -rf {} %s/ \;' % collect_dir
        return cmd

    def generate_host_post_info(self, host_ip, type):
        host_post_info = HostPostInfo()
        # update inventory
        with open(self.ctl.zstack_home + "/../../../ansible/hosts") as f:
            old_hosts = f.read()
            if host_ip not in old_hosts:
                with open(self.ctl.zstack_home + "/../../../ansible/hosts", "w") as f:
                    new_hosts = host_ip + "\n" + old_hosts
                    f.write(new_hosts)
        if type == "mn":
            host_post_info.remote_user = 'root'
            # this will be changed in the future
            host_post_info.remote_port = '22'
            host_post_info.host = host_ip
            host_post_info.host_inventory = self.ctl.zstack_home + "/../../../ansible/hosts"
            host_post_info.post_url = ""
            host_post_info.private_key = self.ctl.zstack_home + "/WEB-INF/classes/ansible/rsaKeys/id_rsa"
            return host_post_info
        (host_user, host_password, host_port) = self.get_host_ssh_info(host_ip, type)
        if host_user != 'root' and host_password is not None:
            host_post_info.become = True
            host_post_info.remote_user = host_user
            host_post_info.remote_pass = host_password
        host_post_info.remote_port = host_port
        host_post_info.host = host_ip
        host_post_info.host_inventory = self.ctl.zstack_home + "/../../../ansible/hosts"
        host_post_info.private_key = self.ctl.zstack_home + "/WEB-INF/classes/ansible/rsaKeys/id_rsa"
        host_post_info.post_url = ""
        return host_post_info

    def get_host_ssh_info(self, host_ip, type):
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        query = MySqlCommandLineQuery()
        query.host = db_hostname
        query.port = db_port
        query.user = db_user
        query.password = db_password
        query.table = 'zstack'
        if type == 'host' or type == 'sharedblock':
            query.sql = "select * from HostVO where managementIp='%s'" % host_ip
            host_uuid = query.query()[0]['uuid']
            query.sql = "select * from KVMHostVO where uuid='%s'" % host_uuid
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['port']
            return (username, password, ssh_port)
        elif type == 'sftp-bs':
            query.sql = "select * from SftpBackupStorageVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == 'ceph-bs':
            query.sql = "select * from CephBackupStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == 'imageStore-bs':
            query.sql = "select * from ImageStoreBackupStorageVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "ceph-ps":
            query.sql = "select * from CephPrimaryStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "vrouter":
            query.sql = "select value from GlobalConfigVO where name='vrouter.password'"
            password = query.query()
            username = "vyos"
            ssh_port = 22
            return (username, password, ssh_port)
        elif type == "pxeserver":
            query.sql = "select * from BaremetalPxeServerVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        else:
            warn("unknown target type: %s" % type)

    def generate_tar_ball(self, run_command_dir, detail_version, time_stamp):
        info_verbose("Compressing log files ...")
        (status, output) = commands.getstatusoutput("cd %s && tar zcf collect-log-%s-%s.tar.gz collect-log-%s-%s"
                                                    % (run_command_dir, detail_version, time_stamp, detail_version,
                                                       time_stamp))
        if status != 0:
            error("Generate tarball failed: %s " % output)

    def compress_and_fetch_log(self, local_collect_dir, tmp_log_dir, host_post_info, type):
        command = "cd %s && tar zcf ../%s-collect-log.tar.gz . --ignore-failed-read --warning=no-file-changed || true" % (
            tmp_log_dir, type)
        run_remote_command(command, host_post_info)
        fetch_arg = FetchArg()
        fetch_arg.src = "%s../%s-collect-log.tar.gz " % (tmp_log_dir, type)
        fetch_arg.dest = local_collect_dir
        fetch_arg.args = "fail_on_missing=yes flat=yes"
        fetch(fetch_arg, host_post_info)
        command = "rm -rf %s../%s-collect-log.tar.gz %s" % (tmp_log_dir, type, tmp_log_dir)
        run_remote_command(command, host_post_info)
        (status, output) = commands.getstatusoutput(
            "cd %s && tar zxf %s-collect-log.tar.gz" % (local_collect_dir, type))
        if status != 0:
            warn("Uncompress %s%s-collect-log.tar.gz meet problem: %s" % (local_collect_dir, type, output))

        commands.getstatusoutput("rm -f %s%s-collect-log.tar.gz" % (local_collect_dir, type))

    def add_collect_thread(self, type, params):
        if "vrouter" in params:
            params.append(self.vrouter_tmp_log_path)

        if type == self.host_type:
            thread = threading.Thread(target=self.get_host_log, args=(params))
        elif type == self.local_type:
            thread = threading.Thread(target=self.get_local_log, args=(params))
        else:
            return
        thread.daemon = True
        self.threads.append(thread)

    def thread_run(self, timeout):
        for t in self.threads:
            t.start()
            while True:
                if len(threading.enumerate()) <= int(self.max_thread_num):
                    break
        for t in self.threads:
            t.join(timeout)

    def get_mn_list(self):
        def find_value_from_conf(content, key, begin, end):
            try:
                idx1 = str(content).index(key)
                sub1 = content[idx1 + len(key):]

                idx2 = sub1.index(begin)
                sub2 = sub1[idx2 + len(begin):]

                idx3 = sub2.index(end)
                return sub2[:idx3].strip('\t\r\n ')
            except Exception as e:
                logger.warn("get ha mn ip failed, please check keepalived conf, %s" % e)
                return "localhost"

        def decode_kp_conf():
            content = linux.read_file(self.HA_KEEPALIVED_CONF)
            ha_mn_list.add(find_value_from_conf(content, "unicast_src_ip", " ", "\n"))
            ha_mn_list.add(find_value_from_conf(content, "unicast_peer", "{", "}"))

        ha_mn_list = set()
        if not os.path.exists(self.HA_KEEPALIVED_CONF):
            ha_mn_list.add("localhost")
        else:
            decode_kp_conf()

        return ha_mn_list

    def collect_configure_log(self, host_list, log_list, collect_dir, type):
        if isinstance(host_list, str):
            if host_list is None:
                return
            if host_list == 'localhost' or host_list == get_default_ip():
                self.add_collect_thread(self.local_type, [log_list, collect_dir, type])
                return
            else:
                self.add_collect_thread(self.host_type,
                                        [self.generate_host_post_info(host_list, type), log_list, collect_dir, type])
                return
        if isinstance(host_list, dict):
            if host_list['exec'] is not None:
                if type == 'mn' and host_list['exec'] == 'AutoCollect':
                    host_list = self.get_mn_list()
                else:
                    exec_cmd = self.get_host_sql(host_list['exec']) + ' | awk \'NR>1\''
                    try:
                        (status, output) = commands.getstatusoutput(exec_cmd)
                        if status == 0 and output.startswith('ERROR') is not True:
                            host_list = output.split('\n')
                        else:
                            error_verbose('fail to exec %s' % host_list['exec'])
                    except Exception:
                        error_verbose('fail to exec %s' % host_list['exec'])

        host_list = list(set(host_list))
        for host_ip in host_list:
            if host_ip is None or host_ip == '':
                return
            if host_ip == 'localhost' or host_ip == get_default_ip():
                self.add_collect_thread(self.local_type, [log_list, collect_dir, type])
            else:
                self.add_collect_thread(self.host_type,
                                        [self.generate_host_post_info(host_ip, type), log_list, collect_dir, type])

    @ignoreerror
    def get_local_log(self, log_list, collect_dir, type):
        if self.check:
            for log in log_list:
                if 'exec' in log:
                    continue
                else:
                    if os.path.exists(log['dir']):
                        command = self.build_collect_cmd(log, None)
                        (status, output) = commands.getstatusoutput(command)
                        if status == 0:
                            key = "%s:%s:%s" % (type, 'localhost', log['name'])
                            self.check_result[key] = output
        else:
            info_verbose("Collecting log from %s localhost ..." % type)
            start = datetime.now()
            local_collect_dir = collect_dir + '%s-%s/' % (type, get_default_ip())
            try:
                # file system broken shouldn't block collect log process
                if not os.path.exists(local_collect_dir):
                    os.makedirs(local_collect_dir)
                for log in log_list:
                    dest_log_dir = local_collect_dir
                    if 'name' in log:
                        dest_log_dir = local_collect_dir + '%s/' % log['name']
                        if not os.path.exists(dest_log_dir):
                            os.makedirs(dest_log_dir)
                    if 'exec' in log:
                        command = log['exec']
                        file_path = dest_log_dir + '%s' % (log['name'])
                        exec_type = log['exec_type']
                        exec_cmd = None
                        if exec_type == 'RunAndRedirect':
                            exec_cmd = '(%s) > %s' % (command, file_path)
                        if exec_type == 'CdAndRun':
                            exec_cmd = 'cd %s && %s' % (dest_log_dir, command)
                        (status, output) = commands.getstatusoutput(exec_cmd)
                        if status == 0:
                            self.add_success_count()
                            logger.info(
                                "exec shell %s successfully!You can check the file at %s" % (command, file_path))
                        elif type != 'sharedblock':
                            self.add_fail_count(1, type, get_default_ip(), log['name'], output)
                    else:
                        if os.path.exists(log['dir']):
                            command = self.build_collect_cmd(log, dest_log_dir)
                            (status, output) = commands.getstatusoutput(command)
                            if status == 0:
                                self.add_success_count()
                                command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % dest_log_dir
                                (status, output) = commands.getstatusoutput(command)
                                if "The directory is empty" in output:
                                    warn("Didn't find log [%s] on %s localhost" % (log['name'], type))
                                    logger.warn("Didn't find log [%s] on %s" % (log['name'], type))
                            else:
                                self.add_fail_count(1, type, get_default_ip(), log['name'], output)
                        else:
                            self.add_fail_count(1, type, get_default_ip(), log['name'],
                                                "the dir path %s did't find on %s localhost" % (log['dir'], type))
                            logger.warn("the dir path %s did't find on %s localhost" % (log['dir'], type))
                            warn("the dir path %s did't find on %s localhost" % (log['dir'], type))
            except SystemExit:
                warn("collect log on localhost failed")
                logger.warn("collect log on localhost failed")
                linux.rm_dir_force(local_collect_dir)
                self.failed_flag = True
                return 1
            end = datetime.now()
            total_collect_time = str(round((end - start).total_seconds(), 1)) + 's'
            self.summary.add_collect_time(type, get_default_ip(), CollectTime(start, end, total_collect_time))
            command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % local_collect_dir
            (status, output) = commands.getstatusoutput(command)
            if "The directory is empty" in output:
                warn("Didn't find log on localhost")
                linux.rm_dir_force(local_collect_dir)
                return 0
            info_verbose("Successfully collect log from %s localhost !" % type)

    def add_success_count(self):
        self.suc_lock.acquire()
        self.summary.success_count += 1
        self.suc_lock.release()

    def add_fail_count(self, fail_log_number, log_type, ip, fail_log_name, fail_cause):
        self.fail_lock.acquire()
        try:
            self.summary.fail_count += fail_log_number
            self.summary.add_fail(log_type, ip, FailDetail(fail_log_name, fail_cause))
        except Exception:
            self.fail_lock.release()
        self.fail_lock.release()

    @ignoreerror
    def get_sharedblock_log(self, host_post_info, tmp_log_dir):
        info_verbose("Collecting sharedblock log from : %s ..." % host_post_info.host)
        target_dir = tmp_log_dir + "sharedblock"
        command = "mkdir -p %s " % target_dir
        run_remote_command(command, host_post_info)

        command = "lsblk -p -o NAME,TYPE,FSTYPE,LABEL,UUID,VENDOR,MODEL,MODE,WWN,SIZE > %s/lsblk_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "ls -l /dev/disk/by-id > %s/ls_dev_disk_by-id_info && echo || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "ls -l /dev/disk/by-path >> %s/ls_dev_disk_by-id_info && echo || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "multipath -ll -v3 >> %s/ls_dev_disk_by-id_info || true" % target_dir
        run_remote_command(command, host_post_info)

        command = "cp /var/log/sanlock.log* %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp -r /var/log/zstack/zsblk-agent/ %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp /var/log/lvmlock/lvmlockd.log* %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "lvmlockctl -i > %s/lvmlockctl_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "sanlock client status -D > %s/sanlock_client_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "sanlock client host_status -D > %s/sanlock_host_info || true" % target_dir
        run_remote_command(command, host_post_info)

        command = "lvs --nolocking -oall > %s/lvm_lvs_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "vgs --nolocking -oall > %s/lvm_vgs_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "lvmconfig --type diff > %s/lvm_config_diff_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp -r /etc/lvm/ %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp -r /etc/sanlock %s || true" % target_dir
        run_remote_command(command, host_post_info)

    def check_host_reachable_in_queen(self, host_post_info):
        self.check_lock.acquire()
        result = check_host_reachable(host_post_info)
        self.check_lock.release()
        return result

    @ignoreerror
    def get_host_log(self, host_post_info, log_list, collect_dir, type, tmp_path = "/tmp"):
        if self.check_host_reachable_in_queen(host_post_info) is True:
            if self.check:
                for log in log_list:
                    if 'exec' in log:
                        continue
                    else:
                        if file_dir_exist("path=%s" % log['dir'], host_post_info):
                            command = self.build_collect_cmd(log, None)
                            (status, output) = run_remote_command(command, host_post_info, return_status=True,
                                                                  return_output=True)
                            if status is True:
                                key = "%s:%s:%s" % (type, host_post_info.host, log['name'])
                                self.check_result[key] = output
            else:
                info_verbose("Collecting log from %s %s ..." % (type, host_post_info.host))
                start = datetime.now()
                local_collect_dir = collect_dir + '%s-%s/' % (type, host_post_info.host)
                tmp_log_dir = "%s/%s-tmp-log/" % (tmp_path, type)
                try:
                    # file system broken shouldn't block collect log process
                    if not os.path.exists(local_collect_dir):
                        os.makedirs(local_collect_dir)
                    command = "mkdir -p %s " % tmp_log_dir
                    run_remote_command(command, host_post_info)
                    for log in log_list:
                        dest_log_dir = tmp_log_dir
                        if 'name' in log:
                            command = "mkdir -p %s" % tmp_log_dir + '%s/' % log['name']
                            run_remote_command(command, host_post_info)
                            dest_log_dir = tmp_log_dir + '%s/' % log['name']
                        if 'exec' in log:
                            command = log['exec']
                            file_path = dest_log_dir + '%s' % (log['name'])
                            exec_type = log['exec_type']
                            exec_cmd = None
                            if exec_type == 'RunAndRedirect':
                                exec_cmd = '(%s) > %s' % (command, file_path)
                            if exec_type == 'CdAndRun':
                                exec_cmd = 'cd %s && %s' % (dest_log_dir, command)
                            (status, output) = run_remote_command(exec_cmd,
                                                                  host_post_info, return_status=True,
                                                                  return_output=True)
                            if status is True:
                                self.add_success_count()
                                logger.info(
                                    "exec shell %s successfully!You can check the file at %s" % (command, file_path))
                            elif type != 'sharedblock':
                                self.add_fail_count(1, type, host_post_info.host, log['name'], output)
                        else:
                            if file_dir_exist("path=%s" % log['dir'], host_post_info):
                                command = self.build_collect_cmd(log, dest_log_dir)
                                (status, output) = run_remote_command(command, host_post_info, return_status=True,
                                                                      return_output=True)
                                if status is True:
                                    self.add_success_count()
                                    command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % dest_log_dir
                                    (status, output) = run_remote_command(command, host_post_info, return_status=True,
                                                                          return_output=True)
                                    if "The directory is empty" in output:
                                        warn("Didn't find log [%s] on %s %s" % (log['name'], type, host_post_info.host))
                                        logger.warn(
                                            "Didn't find log [%s] on %s %s" % (log['name'], type, host_post_info.host))
                                else:
                                    self.add_fail_count(1, type, host_post_info.host, log['name'], output)
                            else:
                                self.add_fail_count(1, type, host_post_info.host, log['name'], "the dir path %s did't find on %s %s" % (
                                    log['dir'], type, host_post_info.host))
                                logger.warn(
                                    "the dir path %s did't find on %s %s" % (log['dir'], type, host_post_info.host))
                                warn("the dir path %s did't find on %s %s" % (log['dir'], type, host_post_info.host))
                except SystemExit:
                    warn("collect log on host %s failed" % host_post_info.host)
                    logger.warn("collect log on host %s failed" % host_post_info.host)
                    command = linux.rm_dir_force(tmp_log_dir, True)
                    self.failed_flag = True
                    run_remote_command(command, host_post_info)
                    return 1

                end = datetime.now()
                total_collect_time = str(round((end - start).total_seconds(), 1)) + 's'
                self.summary.add_collect_time(
                    type, host_post_info.host, CollectTime(start, end, total_collect_time))
                command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % tmp_log_dir
                (status, output) = run_remote_command(command, host_post_info, return_status=True, return_output=True)
                if "The directory is empty" in output:
                    warn("Didn't find log on host: %s " % (host_post_info.host))
                    command = linux.rm_dir_force(tmp_log_dir, True)
                    run_remote_command(command, host_post_info)
                    return 0
                self.compress_and_fetch_log(local_collect_dir, tmp_log_dir, host_post_info, type)
                info_verbose("Successfully collect log from %s %s!" % (type, host_post_info.host))
        else:
            warn("%s %s is unreachable!" % (type, host_post_info.host))
            self.add_fail_count(len(log_list), type, host_post_info.host, 'unreachable',
                                ("%s %s is unreachable!" % (type, host_post_info.host)))

    def get_total_size(self):
        values = self.check_result.values()
        total_size = 0
        for num in values:
            if num is None:
                continue
            elif num.endswith('K'):
                total_size += float(num[:-1])
            elif num.endswith('M'):
                total_size += float(num[:-1]) * 1024
            elif num.endswith('G'):
                total_size += float(num[:-1]) * 1024 * 1024
        total_size = str(round((total_size / 1024 / 1024), 2)) + 'G'
        print '%-50s%-50s' % ('TotalSize(exclude exec statements)', colored(total_size, 'green'))
        for key in sorted(self.check_result.keys()):
            print '%-50s%-50s' % (key, colored(self.check_result[key], 'green'))

    def format_date(self, str_date):
        try:
            d_arr = str_date.split('_')
            if len(d_arr) == 1 or len(d_arr) == 2:
                ymd_array = d_arr[0].split('-')
                if len(ymd_array) == 3:
                    year = ymd_array[0]
                    month = ymd_array[1]
                    day = ymd_array[2]
                    if len(d_arr) == 1:
                        return datetime(int(year), int(month), int(day)).strftime('%Y-%m-%d:%H:%M:%S')
                    else:
                        hms_array = d_arr[1].split(':')
                        hour = hms_array[0] if len(hms_array) > 0 is not None else '00'
                        minute = hms_array[1] if len(hms_array) > 1 is not None else '00'
                        sec = hms_array[2] if len(hms_array) > 2 is not None else '00'
                        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(sec)) \
                            .strftime('%Y-%m-%d:%H:%M:%S')
                else:
                    error_verbose(
                        "make sure the date [%s] is correct and in \'yyyy-MM-dd\' or \'yyyy-MM-dd_hh:mm:ss\' format" % str_date)
            else:
                error_verbose(
                    "make sure the date [%s] is correct and in \'yyyy-MM-dd\' or \'yyyy-MM-dd_hh:mm:ss\' format" % str_date)
        except ValueError:
            error_verbose(
                "make sure the date [%s] is correct and in \'yyyy-MM-dd\' or \'yyyy-MM-dd_hh:mm:ss\' format" % str_date)

    def param_validate(self, args):
        if args.since is None:
            if args.from_date is None:
                self.f_date = (datetime.now() + timedelta(days=-1)).strftime('%Y-%m-%d:%H:%M:%S')
            elif args.from_date == '-1':
                self.f_date = '0000-00-00:00:00'
            else:
                self.f_date = self.format_date(args.from_date)
            if args.to_date is not None and args.to_date != '-1':
                self.t_date = self.format_date(args.to_date)
            else:
                self.t_date = datetime.now().strftime('%Y-%m-%d:%H:%M:%S')
        else:
            try:
                if args.since.endswith('d') or args.since.endswith('D'):
                    self.f_date = (datetime.now() + timedelta(days=float('-%s' % (args.since[:-1])))).strftime(
                        '%Y-%m-%d:%H:%M:%S')
                elif args.since.endswith('h') or args.since.endswith('H'):
                    self.f_date = (datetime.now() + timedelta(
                        days=float('-%s' % round(float(args.since[:-1]) / 24, 2)))).strftime('%Y-%m-%d:%H:%M:%S')
                else:
                    error_verbose("error since format:[%s], correct format example '--since 2d'" % args.since)
                self.t_date = datetime.now().strftime('%Y-%m-%d:%H:%M:%S')
            except ValueError:
                error_verbose("error since format:[%s], correct format example '--since 2d'" % args.since)

        if self.f_date > self.t_date:
            error_verbose("from datetime [%s] can not be later than to datetime [%s]" % (self.f_date, self.t_date))

        if args.check:
            self.check = True
            info_verbose("Start checking the file size ,,,")
        else:
            self.check = False

        if args.thread and not str(args.thread).isdigit():
            error_verbose("thread number must be a positive integer")
        if args.thread and int(args.thread) < 2:
            error_verbose("at least 2 threads")
        if args.timeout and not str(args.timeout).isdigit():
            error_verbose("timeout must be a positive integer")

    def run(self, collect_dir, detail_version, time_stamp, args):
        zstack_path = os.environ.get('ZSTACK_HOME', None)
        if zstack_path and zstack_path != self.DEFAULT_ZSTACK_HOME:
            self.DEFAULT_ZSTACK_HOME = zstack_path

        run_command_dir = os.getcwd()
        if not os.path.exists(collect_dir) and args.check is not True:
            os.makedirs(collect_dir)

        self.param_validate(args)

        if args.thread is not None:
            self.max_thread_num = args.thread

        decode_result = self.decode_conf_yml(args)

        if decode_result['decode_error'] is not None:
            error_verbose(decode_result['decode_error'])

        for key, value in decode_result.items():
            if key == 'decode_error':
                continue
            else:
                self.collect_configure_log(value['list'], value['logs'], collect_dir, key)
        self.thread_run(int(args.timeout))
        if self.check:
            self.get_total_size()
        else:
            self.summary.persist(collect_dir)
            if len(threading.enumerate()) > 1:
                info_verbose("It seems that some collect log thread timeout, "
                             "if compress failed, please use \'cd %s && tar zcf collect-log-%s-%s.tar.gz collect-log-%s-%s\' manually"
                             % (run_command_dir, detail_version, time_stamp, detail_version, time_stamp))
            self.generate_tar_ball(run_command_dir, detail_version, time_stamp)
            if self.failed_flag is True:
                info_verbose("The collect log generate at: %s.tar.gz,success %s,fail %s" % (
                    collect_dir, self.summary.success_count, self.summary.fail_count))
                info_verbose(colored("Please check the reason of failed task in log: %s\n" % (
                        self.logger_dir + self.logger_file), 'yellow'))
            else:
                info_verbose("The collect log generate at: %s/collect-log-%s-%s.tar.gz,success %s,fail %s" % (
                    run_command_dir, detail_version, time_stamp, self.summary.success_count, self.summary.fail_count))
