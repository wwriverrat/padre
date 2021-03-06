from __future__ import print_function

import argparse
import errno
import json
import logging
from logging import config as logging_config
import os
import signal
import sys

import six
from six.moves import urllib

import munch
import pytz
import tzlocal
import yaml

from padre import bot
from padre import date_utils as du
from padre import event
from padre import process_utils as pu
from padre import utils

LOG = logging.getLogger(__name__)


def load_yaml_or_secret_yaml(path, force_secrets=False):
    try:
        path, env_lookup_key = path.split(":", 1)
    except ValueError:
        env_lookup_key = None
    if env_lookup_key or force_secrets:
        cmd = [
            'padre-decoder', '-f', path,
        ]
        if env_lookup_key:
            cmd.extend([
                '-e', env_lookup_key,
            ])
        res = pu.run(cmd, stdout=pu.PIPE)
        res.raise_for_status()
        # In python 3.x the json.loads requires a unicode type...
        res_stdout = res.stdout
        if isinstance(res_stdout, six.binary_type):
            res_stdout = res_stdout.decode("utf8")
        data = json.loads(res_stdout)
    else:
        with open(path, "rb") as fh:
            data = yaml.safe_load(fh.read())
    return data


def setup_ansible(config, secrets):
    try:
        ansible_tpl_path = config.ansible_config_template_path
    except AttributeError:
        ansible_tpl_path = None
    try:
        ansible_cfg_path = config.ansible_config_path
    except AttributeError:
        ansible_cfg_path = None
    if ansible_cfg_path and ansible_tpl_path:
        with open(ansible_tpl_path, 'rb') as fh:
            ansible_cfg_tpl_params = munch.unmunchify(config)
            ansible_cfg = utils.render_template(fh.read(),
                                                ansible_cfg_tpl_params)
        utils.safe_make_dirs(os.path.dirname(ansible_cfg_path))
        with open(ansible_cfg_path, 'wb') as fh:
            fh.write(ansible_cfg)


def setup_ssh(config, secrets):
    def iter_lines_clean(blob):
        for line in blob.splitlines():
            if not line or line.startswith("#"):
                continue
            else:
                yield line
    try:
        create_at = config.ssh.create_at
    except AttributeError:
        create_at = None
    if not create_at:
        return
    try:
        os.makedirs(create_at)
    except OSError as e:
        if e.errno == errno.EEXIST:
            if not os.path.isdir(create_at):
                raise
        else:
            raise
    try:
        ssh_conf = config.ssh.config
    except AttributeError:
        ssh_conf = None
    if ssh_conf:
        ssh_conf_path = os.path.join(create_at, "config")
        with open(ssh_conf_path, 'wb') as fh:
            fh.write(ssh_conf)
        os.chmod(ssh_conf_path, 0o600)
    try:
        ssh_priv = config.ssh.private_key
    except AttributeError:
        ssh_priv = None
    if ssh_priv:
        ssh_priv_path = os.path.join(create_at, "id_rsa")
        with open(ssh_priv_path, 'wb') as fh:
            fh.write(ssh_priv)
        os.chmod(ssh_priv_path, 0o600)
    try:
        ssh_pub = config.ssh.public_key
    except AttributeError:
        ssh_pub = None
    if ssh_pub:
        ssh_pub_path = os.path.join(create_at, "id_rsa.pub")
        with open(ssh_pub_path, 'wb') as fh:
            fh.write(ssh_pub)
        os.chmod(ssh_pub_path, 0o600)
    try:
        known_hosts = config.ssh.known_hosts
    except AttributeError:
        known_hosts = ()
    known_hosts_lines = []
    for host in known_hosts:
        scan_command = ['ssh-keyscan']
        # Use urlparse and fake an https address using the given host.
        # This works well with both hostnames and IPs (v4/v6), and ALSO ports.
        parsed = urllib.parse("https://{}".format(host))
        if parsed.port:
            scan_command.extend(['-p', parsed.port])
        scan_command.append(parsed.hostname)
        r = pu.run(scan_command, stdout=pu.PIPE, stderr=pu.PIPE)
        r.raise_for_status()
        known_hosts_lines.append("# Keyscan for '%s'" % host)
        known_hosts_lines.extend(iter_lines_clean(r.stdout))
    try:
        fetcher_func = config.plugins.env_fetcher_func
    except AttributeError:
        fetcher_func = None
    else:
        fetcher_func = utils.import_func(fetcher_func)
    render_bin = utils.find_executable("render")
    if render_bin and fetcher_func:
        for env_name, env_topo_fn in fetcher_func(
                env_dir=config.get("env_dir")):
            r = pu.run([render_bin, '-e', env_topo_fn, 'known_hosts'],
                       stdout=pu.PIPE, stderr=pu.PIPE)
            r.raise_for_status()
            known_hosts_lines.append("# Environment '%s'" % env_name)
            known_hosts_lines.extend(iter_lines_clean(r.stdout))
    if known_hosts_lines:
        known_hosts_path = os.path.join(create_at, "known_hosts")
        with open(known_hosts_path, 'wb') as fh:
            fh.write(("# WARNING: DO NOT EDIT THIS"
                      " FILE (IT WAS AUTOGENERATED ON BOT BOOTSTRAP!!!)\n"))
            fh.write("\n".join(known_hosts_lines))
        os.chmod(known_hosts_path, 0o644)


def setup_dirs(config, secrets):
    for k in ('working_dir', 'persistent_working_dir'):
        try:
            k_path = config[k]
        except KeyError:
            pass
        else:
            utils.safe_make_dirs(k_path)


def setup_certs(config, secrets):
    try:
        hook_cert = config.ssl.cert
    except AttributeError:
        hook_cert = None
    try:
        hook_pem = config.ssl.private_key
    except AttributeError:
        hook_pem = None
    for item in [hook_cert, hook_pem]:
        if not item:
            continue
        item_path = item.get("path")
        item_contents = item.get("contents")
        if item_path and item_contents:
            with open(item_path, 'wb') as fh:
                fh.write(item_contents)


def setup_git(config, secrets):
    try:
        git_email = config.admin_email
    except AttributeError:
        git_email = None
    if git_email:
        git_user = git_email.split('@')[0]
        set_email_cmd = ('git', 'config', '--global', 'user.email', git_email)
        set_name_cmd = ('git', 'config', '--global', 'user.name', git_user)
        res = pu.run(set_email_cmd)
        res.raise_for_status()
        res = pu.run(set_name_cmd)
        res.raise_for_status()
    try:
        gerrit_user = config.launchpad.user
    except AttributeError:
        gerrit_user = None
    if gerrit_user:
        set_gerrit_cmd = ('git', 'config', '--global', 'gitreview.username',
                          gerrit_user)
        res = pu.run(set_gerrit_cmd)
        res.raise_for_status()


def run_bot(config, secrets):
    main_bot = bot.Bot(config, secrets)

    def do_restart(*args, **kwargs):
        LOG.info("Signaling padre bot to restart")
        main_bot.dead.set(val=event.Event.RESTART)

    def do_death(*args, **kwargs):
        LOG.info("Signaling padre bot to die")
        main_bot.dead.set(val=event.Event.DIE)

    LOG.info("Setting up signal handlers")
    signal.signal(signal.SIGTERM, do_death)
    signal.signal(signal.SIGINT, do_death)

    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, do_restart)

    LOG.info("Starting padre bot")
    while True:
        should_restart = main_bot.run()
        if should_restart:
            LOG.info("Restarting padre bot")
        else:
            break


def iter_identify_paths(paths):
    for path in paths:
        try:
            path, env_lookup_key = path.split(":", 1)
            path = path.strip()
        except ValueError:
            env_lookup_key = None
            path = path.strip()
        if not path or not os.path.exists(path):
            continue
        if os.path.isfile(path):
            yield (path, env_lookup_key, 'f')
        elif os.path.isdir(path):
            yield (path, env_lookup_key, 'd')
        else:
            raise RuntimeError("Unable to iterate paths from"
                               " unknown path '%s' (it is not a directory"
                               " or a file)" % path)


def iter_paths(paths, ok_extensions=('.yaml', '.yml')):
    seen_paths = set()
    for path, env_lookup_key, kind in paths:
        if kind == 'f':
            if env_lookup_key:
                path = path + ":" + env_lookup_key
            if path not in seen_paths:
                seen_paths.add(path)
                yield path
        else:
            # NOTE: this does not recurse (and that should be ok...)
            for sub_path in os.listdir(path):
                if sub_path.startswith("."):
                    continue
                sub_path = os.path.join(path, sub_path)
                sub_path_base, sub_path_ext = os.path.splitext(sub_path)
                sub_path_ext = sub_path_ext.lower()
                if sub_path_ext in ok_extensions and os.path.isfile(sub_path):
                    if env_lookup_key:
                        sub_path = sub_path + ":" + env_lookup_key
                    if sub_path not in seen_paths:
                        seen_paths.add(sub_path)
                        yield sub_path


def run_bootstrap(config, secrets):
    LOG.info("Starting bootstrap")
    for boot_func in (setup_dirs, setup_ssh, setup_certs,
                      setup_ansible, setup_git):
        LOG.info("Running '%s'", boot_func.__name__)
        boot_func(config, secrets)


def main():
    parser = argparse.ArgumentParser("OpenStack Chat Operator")
    parser.add_argument("-c", "--config",
                        help=("configuration (or secrets) file or directory"
                              " (containing yaml files) to load (merged into"
                              " bot configuration)"),
                        required=True, metavar='PATH',
                        action='append', default=[])
    parser.add_argument("-s", "--secrets",
                        help=("secrets file or directory (containing"
                              " yaml files) to load (not merged into"
                              " bot configuration)"),
                        metavar='FILE', action='append', default=[])
    parser.add_argument("--just-bootstrap", default=False,
                        help="bootstrap and do not run main bot loop",
                        action='store_true')

    args = parser.parse_args()

    config = {}
    for i, path in enumerate(iter_paths(iter_identify_paths(args.config))):
        if i == 0:
            print("Loading configuration from '%s'" % path, file=sys.stderr)
        else:
            print("Loading + merging configuration"
                  " from '%s'" % path, file=sys.stderr)
        tmp_config = load_yaml_or_secret_yaml(path, force_secrets=False)
        config = utils.merge_dict(config, tmp_config)
    config = munch.munchify(config)

    print("Configuration: %s" %
          json.dumps(utils.mask_dict_password(config),
                     indent=4, sort_keys=True), file=sys.stderr)
    secrets = {}
    for i, path in enumerate(iter_paths(iter_identify_paths(args.secrets))):
        if i == 0:
            print("Loading secrets from '%s'" % path, file=sys.stderr)
        else:
            print("Loading + merging secrets"
                  " from '%s'" % path, file=sys.stderr)
        tmp_secrets = load_yaml_or_secret_yaml(path, force_secrets=True)
        secrets = utils.merge_dict(secrets, tmp_secrets)
    secrets = munch.munchify(secrets)

    tz = config.get("tz")
    if not tz:
        try:
            tmp_tz = tzlocal.get_localzone()
        except (IOError, pytz.UnknownTimeZoneError):
            pass
        else:
            if tmp_tz:
                tz = tmp_tz.zone
    if not tz:
        tz = du.DEFAULT_TZ
    try:
        config['tz'] = tz
        pytz.timezone(config.tz)
    except pytz.UnknownTimeZoneError:
        parser.error("Config is missing a valid timezone/tz")

    print("Switching to configured (or default) logging", file=sys.stderr)
    try:
        log_config = config['logging']
    except KeyError:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)-15s %(levelname)s:%(name)s:%(message)s')
    else:
        logging_config.dictConfig(log_config)

    if args.just_bootstrap:
        run_bootstrap(config, secrets)
    else:
        run_bot(config, secrets)


if __name__ == '__main__':
    main()
