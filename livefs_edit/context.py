import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile

from . import run


class _MountBase:

    def p(self, *args):
        for a in args:
            if a.startswith('/'):
                raise Exception('no absolute paths here please')
        return os.path.join(self.mountpoint, *args)

    def write(self, path, content):
        with open(self.p(path), 'w') as fp:
            fp.write(content)


class Mountpoint(_MountBase):
    def __init__(self, *, device, mountpoint):
        self.device = device
        self.mountpoint = mountpoint


class OverlayMountpoint(_MountBase):
    def __init__(self, *, lowers, upperdir, mountpoint):
        self.lowers = lowers
        self.upperdir = upperdir
        self.mountpoint = mountpoint

    def unchanged(self):
        return os.listdir(self.upperdir) == []


class EditContext:

    def __init__(self, iso_path):
        self.iso_path = iso_path
        self._iso_overlay = None
        self.dir = tempfile.mkdtemp()
        os.mkdir(self.p('.tmp'))
        self._cache = {}
        self._indent = ''
        self._pre_repack_hooks = []
        self._mounts = []
        self._squash_mounts = {}
        self._copy_on_teardown = None

    def log(self, msg):
        print(self._indent + msg)

    @contextlib.contextmanager
    def logged(self, msg, done_msg=None):
        self.log(msg)
        self._indent += '  '
        try:
            yield
        finally:
            self._indent = self._indent[:-2]
        if done_msg is not None:
            self.log(done_msg)

    def tmpdir(self):
        d = tempfile.mkdtemp(dir=self.p('.tmp'))
        os.chmod(d, 0o755)
        return d

    def tmpfile(self):
        return tempfile.mktemp(dir=self.p('.tmp'))

    def p(self, *args, allow_abs=False):
        if not allow_abs:
            for a in args:
                if a.startswith('/'):
                    raise Exception('no absolute paths here please')
        return os.path.join(self.dir, *args)

    def add_mount(self, typ, src, mountpoint, *, options=None):
        cmd = ['mount']
        if typ is not None:
            cmd.extend(['-t', typ])
        cmd.append(src)
        if options:
            cmd.extend(['-o', options])
        if mountpoint is None:
            mountpoint = self.tmpdir()
        cmd.append(mountpoint)
        if not os.path.isdir(mountpoint):
            os.makedirs(mountpoint)
        run(cmd)
        self._mounts.append(mountpoint)
        return Mountpoint(device=src, mountpoint=mountpoint)

    def umount(self, mountpoint):
        self._mounts.remove(mountpoint)
        run(['umount', mountpoint])

    def add_sys_mounts(self, mountpoint):
        mnts = []
        for typ, relpath in [
                ('devtmpfs',   'dev'),
                ('devpts',     'dev/pts'),
                ('proc',       'proc'),
                ('sysfs',      'sys'),
                ('securityfs', 'sys/kernel/security'),
                ]:
            mnts.append(self.add_mount(typ, typ, f'{mountpoint}/{relpath}'))
        resolv_conf = f'{mountpoint}/etc/resolv.conf'
        os.rename(resolv_conf, resolv_conf + '.tmp')
        shutil.copy('/etc/resolv.conf', resolv_conf)

        def _pre_repack():
            for mnt in reversed(mnts):
                self.umount(mnt.p())
            os.rename(resolv_conf + '.tmp', resolv_conf)

        self.add_pre_repack_hook(_pre_repack)

    def add_overlay(self, lowers, mountpoint=None):
        if not isinstance(lowers, list):
            lowers = [lowers]
        upperdir = self.tmpdir()
        workdir = self.tmpdir()

        def lowerdir_for(lower):
            if isinstance(lower, str):
                return lower
            if isinstance(lower, Mountpoint):
                return lower.p()
            if isinstance(lower, OverlayMountpoint):
                return lowerdir_for(lower.lowers + [lower.upperdir])
            if isinstance(lower, list):
                return ':'.join(reversed([lowerdir_for(ll) for ll in lower]))
            raise Exception(f'lowerdir_for({lower!r})')

        lowerdir = lowerdir_for(lowers)
        options = f'lowerdir={lowerdir},upperdir={upperdir},workdir={workdir}'
        return OverlayMountpoint(
            lowers=lowers,
            mountpoint=self.add_mount(
                'overlay', 'overlay', mountpoint, options=options).p(),
            upperdir=upperdir)

    def add_pre_repack_hook(self, hook):
        self._pre_repack_hooks.append(hook)

    def mount_squash(self, name):
        target = self.p('old/' + name)
        squash = self.p(f'old/iso/casper/{name}.squashfs')
        if name in self._squash_mounts:
            return self._squash_mounts[name]
        else:
            self._squash_mounts[name] = m = self.add_mount(
                'squashfs', squash, target, options='ro')
            return m

    def get_arch(self):
        # Is this really the best way??
        with open(self.p('new/iso/.disk/info')) as fp:
            return fp.read().strip().split()[-2]

    def edit_squashfs(self, name, *, add_sys_mounts=True):
        lower = self.mount_squash(name)
        target = self.p(f'new/{name}')
        if os.path.exists(target):
            return target
        overlay = self.add_overlay(lower, target)
        self.log(f"squashfs {name!r} now mounted at {target!r}")
        new_squash = self.p(f'new/iso/casper/{name}.squashfs')

        def _pre_repack():
            try:
                os.unlink(f'{overlay.upperdir}/etc/resolv.conf')
                os.rmdir(f'{overlay.upperdir}/etc')
            except OSError:
                pass
            if overlay.unchanged():
                self.log(f"no changes found in squashfs {name!r}")
                return
            with self.logged(f"repacking squashfs {name!r}"):
                os.unlink(new_squash)
            run(['mksquashfs', target, new_squash])

        self.add_pre_repack_hook(_pre_repack)

        if add_sys_mounts:
            self.add_sys_mounts(target)

        return target

    def teardown(self):
        for mount in reversed(self._mounts):
            run(['mount', '--make-rprivate', mount])
            run(['umount', '-R', mount])

        if self._copy_on_teardown:
            with self.logged("copy contents to mountpoint now"):
                run(['cp', '-aT', self.p('new/iso'), self._copy_on_teardown])
        shutil.rmtree(self.dir)

    def mount_iso(self, already_mounted=False):
        if not already_mounted:
            mountpoint = self.add_mount(
                'iso9660', self.iso_path, self.p('old/iso'),
                options='loop,ro')
        else:
            os.makedirs(self.p('old'), exist_ok=True)
            os.symlink(self.iso_path, self.p('old/iso'))
            mountpoint = Mountpoint(
                device=self.iso_path, mountpoint=self.p('old/iso'))

        self._iso_overlay = self.add_overlay(
            mountpoint,
            self.p('new/iso'))

    def repack_iso(self, destpath):
        with self.logged("running repack hooks"):
            for hook in reversed(self._pre_repack_hooks):
                hook()
        if self._iso_overlay.unchanged():
            self.log("no changes!")
            return
        cp = run(
            ['xorriso', '-indev', self.iso_path, '-report_el_torito',
             'as_mkisofs'],
            encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        opts = shlex.split(cp.stdout)
        with self.logged("recreating ISO"):
            run(['xorriso', '-as', 'mkisofs'] + opts +
                ['-o', destpath, '-V', 'Ubuntu custom', self.p('new/iso')])

    def repack_in_mounted(self, destpath):
        with self.logged("running repack hooks"):
            for hook in reversed(self._pre_repack_hooks):
                hook()
        if self._iso_overlay.unchanged():
            self.log("no changes!")
            return
        with self.logged("preparing to do the final copy"):
            self._copy_on_teardown = destpath
