from cStringIO import StringIO
from configobj import ConfigObj
import base64
import contextlib
import logging
import os

from teuthology import misc as teuthology
from teuthology import contextutil
from ..orchestra import run
from ..orchestra.connection import split_user

log = logging.getLogger(__name__)


@contextlib.contextmanager
def download(ctx, config):
    assert isinstance(config, list)
    log.info('Downloading s3-tests...')
    for client in config:
        ctx.cluster.only(client).run(
            args=[
                'git', 'clone',
#                'https://github.com/NewDreamNetwork/s3-tests.git',
                'http://ceph.newdream.net/git/s3-tests.git',
                '/tmp/cephtest/s3-tests',
                ],
            )
    try:
        yield
    finally:
        log.info('Removing s3-tests...')
        for client in config:
            ctx.cluster.only(client).run(
                args=[
                    'rm',
                    '-rf',
                    '/tmp/cephtest/s3-tests',
                    ],
                )

def _config_user(s3tests_conf, section, user):
    s3tests_conf[section].setdefault('user_id', user)
    s3tests_conf[section].setdefault('email', '{user}+test@test.test'.format(user=user))
    s3tests_conf[section].setdefault('display_name', 'Mr. {user}'.format(user=user))
    s3tests_conf[section].setdefault('access_key', base64.b64encode(os.urandom(20)))
    s3tests_conf[section].setdefault('secret_key', base64.b64encode(os.urandom(40)))

@contextlib.contextmanager
def create_users(ctx, config):
    assert isinstance(config, dict)
    log.info('Creating rgw users...')
    for client in config['clients']:
        s3tests_conf = config['s3tests_conf'][client]
        s3tests_conf.setdefault('fixtures', {})
        s3tests_conf['fixtures'].setdefault('bucket prefix', 'test-' + client + '-{random}-')
        for section, user in [('s3 main', 'foo'), ('s3 alt', 'bar')]:
            _config_user(s3tests_conf, section, '{user}.{client}'.format(user=user, client=client))
            ctx.cluster.only(client).run(
                args=[
                    'LD_LIBRARY_PATH=/tmp/cephtest/binary/usr/local/lib',
                    '/tmp/cephtest/enable-coredump',
                    '/tmp/cephtest/binary/usr/local/bin/ceph-coverage',
                    '/tmp/cephtest/archive/coverage',
                    '/tmp/cephtest/binary/usr/local/bin/radosgw-admin',
                    '-c', '/tmp/cephtest/ceph.conf',
                    'user', 'create',
                    '--uid', s3tests_conf[section]['user_id'],
                    '--display-name', s3tests_conf[section]['display_name'],
                    '--access-key', s3tests_conf[section]['access_key'],
                    '--secret', s3tests_conf[section]['secret_key'],
                    '--email', s3tests_conf[section]['email'],
                ],
            )
    yield


@contextlib.contextmanager
def configure(ctx, config):
    assert isinstance(config, dict)
    log.info('Configuring s3-tests...')
    for client, properties in config['clients'].iteritems():
        s3tests_conf = config['s3tests_conf'][client]
        if properties is not None and 'rgw_server' in properties:
            host = None
            for target, roles in zip(ctx.config['targets'].iterkeys(), ctx.config['roles']):
                log.info('roles: ' + str(roles))
                log.info('target: ' + str(target))
                if properties['rgw_server'] in roles:
                    _, host = split_user(target)
            assert host is not None, "Invalid client specified as the rgw_server"
            s3tests_conf['DEFAULT']['host'] = host
        else:
            s3tests_conf['DEFAULT']['host'] = 'localhost'

        (remote,) = ctx.cluster.only(client).remotes.keys()
        remote.run(
            args=[
                'cd',
                '/tmp/cephtest/s3-tests',
                run.Raw('&&'),
                './bootstrap',
                ],
            )
        conf_fp = StringIO()
        s3tests_conf.write(conf_fp)
        teuthology.write_file(
            remote=remote,
            path='/tmp/cephtest/archive/s3-tests.{client}.conf'.format(client=client),
            data=conf_fp.getvalue(),
            )
    yield


@contextlib.contextmanager
def run_tests(ctx, config):
    assert isinstance(config, dict)
    for client, client_config in config.iteritems():
        args = [
                'S3TEST_CONF=/tmp/cephtest/archive/s3-tests.{client}.conf'.format(client=client),
                '/tmp/cephtest/s3-tests/virtualenv/bin/nosetests',
                '-w',
                '/tmp/cephtest/s3-tests',
                '-v',
                '-a', '!fails_on_rgw',
                ]
        if client_config is not None and 'extra_args' in client_config:
            args.extend(client_config['extra_args'])

        ctx.cluster.only(client).run(
            args=args,
            )
    yield


@contextlib.contextmanager
def task(ctx, config):
    """
    Run the s3-tests suite against rgw.

    To run all tests on all clients::

        tasks:
        - ceph:
        - rgw:
        - s3tests:

    To restrict testing to particular clients::

        tasks:
        - ceph:
        - rgw: [client.0]
        - s3tests: [client.0]

    To run against a server on client.1::

        tasks:
        - ceph:
        - rgw: [client.1]
        - s3tests:
            client.0:
              rgw_server: client.1

    To pass extra arguments to nose (e.g. to run a certain test)::

        tasks:
        - ceph:
        - rgw: [client.0]
        - s3tests:
            client.0:
              extra_args: ['test_s3:test_object_acl_grand_public_read']
            client.1:
              extra_args: ['--exclude', 'test_100_continue']
    """
    assert config is None or isinstance(config, list) \
        or isinstance(config, dict), \
        "task s3tests only supports a list or dictionary for configuration"
    all_clients = ['client.{id}'.format(id=id_)
                   for id_ in teuthology.all_roles_of_type(ctx.cluster, 'client')]
    if config is None:
        config = all_clients
    if isinstance(config, list):
        config = dict.fromkeys(config)
    clients = config.keys()

    s3tests_conf = {}
    for client in clients:
        s3tests_conf[client] = ConfigObj(
            indent_type='',
            infile={
                'DEFAULT':
                    {
                    'port'      : 7280,
                    'is_secure' : 'no',
                    },
                'fixtures' : {},
                's3 main'  : {},
                's3 alt'   : {},
                }
            )

    with contextutil.nested(
        lambda: download(ctx=ctx, config=clients),
        lambda: create_users(ctx=ctx, config=dict(
                clients=clients,
                s3tests_conf=s3tests_conf,
                )),
        lambda: configure(ctx=ctx, config=dict(
                clients=config,
                s3tests_conf=s3tests_conf,
                )),
        lambda: run_tests(ctx=ctx, config=config),
        ):
        yield
