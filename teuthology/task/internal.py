import contextlib
import gevent
import logging
import os
import tarfile

from teuthology import safepath
from orchestra import run

log = logging.getLogger(__name__)

@contextlib.contextmanager
def base(ctx, config):
    log.info('Creating base directory...')
    run.wait(
        ctx.cluster.run(
            args=[
                'mkdir', '-m0755', '--',
                '/tmp/cephtest',
                ],
            wait=False,
            )
        )

    try:
        yield
    finally:
        log.info('Tidying up after the test...')
        # if this fails, one of the earlier cleanups is flawed; don't
        # just cram an rm -rf here
        run.wait(
            ctx.cluster.run(
                args=[
                    'rmdir',
                    '--',
                    '/tmp/cephtest',
                    ],
                wait=False,
                ),
            )


def check_conflict(ctx, config):
    log.info('Checking for old test directory...')
    processes = ctx.cluster.run(
        args=[
            'test', '!', '-e', '/tmp/cephtest',
            ],
        wait=False,
        )
    failed = False
    for proc in processes:
        assert isinstance(proc.exitstatus, gevent.event.AsyncResult)
        try:
            proc.exitstatus.get()
        except run.CommandFailedError:
            log.error('Host %s has stale cephtest directory, check your lock and reboot to clean up.', proc.remote.shortname)
            failed = True
    if failed:
        raise RuntimeError('Stale jobs detected, aborting.')

@contextlib.contextmanager
def archive(ctx, config):
    log.info('Creating archive directory...')
    run.wait(
        ctx.cluster.run(
            args=[
                'install', '-d', '-m0755', '--',
                '/tmp/cephtest/archive',
                ],
            wait=False,
            )
        )

    try:
        yield
    finally:
        if ctx.archive is not None:

            log.info('Transferring archived files...')
            logdir = os.path.join(ctx.archive, 'remote')
            os.mkdir(logdir)
            for remote in ctx.cluster.remotes.iterkeys():
                path = os.path.join(logdir, remote.shortname)
                os.mkdir(path)
                log.debug('Transferring archived files from %s to %s', remote.shortname, path)
                proc = remote.run(
                    args=[
                        'tar',
                        'c',
                        '-f', '-',
                        '-C', '/tmp/cephtest/archive',
                        '--',
                        '.',
                        ],
                    stdout=run.PIPE,
                    wait=False,
                    )
                tar = tarfile.open(mode='r|', fileobj=proc.stdout)
                while True:
                    ti = tar.next()
                    if ti is None:
                        break

                    if ti.isdir():
                        # ignore silently; easier to just create leading dirs below
                        pass
                    elif ti.isfile():
                        sub = safepath.munge(ti.name)
                        safepath.makedirs(root=path, path=os.path.dirname(sub))
                        tar.makefile(ti, targetpath=os.path.join(path, sub))
                    else:
                        if ti.isdev():
                            type_ = 'device'
                        elif ti.issym():
                            type_ = 'symlink'
                        elif ti.islnk():
                            type_ = 'hard link'
                        else:
                            type_ = 'unknown'
                        log.info('Ignoring tar entry: %r type %r', ti.name, type_)
                        continue
                proc.exitstatus.get()

            log.info('Removing archived files...')
            run.wait(
                ctx.cluster.run(
                    args=[
                        'rm',
                        '-rf',
                        '--',
                        '/tmp/cephtest/archive',
                        ],
                    wait=False,
                    ),
                )