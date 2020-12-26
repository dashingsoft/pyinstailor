'''
pyinstailor is a tailor to replace files directly in the executable
file generated by PyInstaller. Sometimes the script is changed a
little, for example, only refine log messages, no dependency changed,
no analysis is required. In this case, pyinstailor could extract the
executable, replace the old script with new one, then repack it.

Usage

    pyinstailor.py [-h] [-d] [-s N] executable [files]

Examples

* List all the available items in the archive

    pyinstailor dist/foo.exe

* Replace entry script "foo.py" in the bundle "foo.exe"

    pyinstailor dist/foo.exe foo.py

* If entry scrip is in other path, strip the first N path

    pyinstailor -s 1 dist/foo.exe src/foo.py

* Replace package file "reader/__init__.py" in the bundle "foo.exe"

    pyinstailor dist/foo.exe reader/__init__.py

* Strip the first path in the file path

    pyinstailor -s 2 dist/foo.exe ../../reader/__init__.py

This tool doesn't support PyInstaller 2.0, and it's not full test.

'''
import argparse
import logging
import marshal
import os
import shutil
import struct
import sys
import zlib

from subprocess import Popen

from PyInstaller.archive.writers import ZlibArchiveWriter, CArchiveWriter
from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.loader.pyimod02_archive import ZlibArchiveReader, PYZ_TYPE_PKG
from PyInstaller.compat import is_darwin, is_linux


logger = logging.getLogger('pyinstailor')


def makedirs(path, exist_ok=False):
    if not (exist_ok and os.path.exists(path)):
        os.makedirs(path)


class ZlibArchive(ZlibArchiveReader):

    def checkmagic(self):
        """ Overridable.
            Check to see if the file object self.lib actually has a file
            we understand.
        """
        self.lib.seek(self.start)  # default - magic is at start of file.
        if self.lib.read(len(self.MAGIC)) != self.MAGIC:
            raise RuntimeError("%s is not a valid %s archive file"
                               % (self.path, self.__class__.__name__))
        if self.lib.read(len(self.pymagic)) != self.pymagic:
            print("Warning: pyz is from a different Python version")
        self.lib.read(4)


class CArchiveWriter2(CArchiveWriter):

    def add(self, entry):
        patched, dlen, ulen, flag, typcd, nm, pathnm = entry
        where = self.lib.tell()

        logger.debug('Handle item "%s"', nm)

        if is_darwin and patched and typcd == 'b':
            from PyInstaller.depend import dylib
            dylib.mac_set_relative_dylib_deps(pathnm, os.path.basename(pathnm))

        fh = open(pathnm, 'rb')
        filedata = fh.read()
        fh.close()

        if patched:
            logger.info('Patch item "%s" with "%s"', nm, pathnm)
            if typcd in ('s', 'M'):
                code = compile(filedata, '<%s>' % nm, 'exec')
                filedata = marshal.dumps(code)
                ulen = len(filedata)
            else:
                ulen = len(filedata)

        if flag == 1 and patched:
            comprobj = zlib.compressobj(self.LEVEL)
            self.lib.write(comprobj.compress(filedata))
            self.lib.write(comprobj.flush())
        else:
            self.lib.write(filedata)

        dlen = self.lib.tell() - where
        self.toc.add(where, dlen, ulen, flag, typcd, nm)


def get_carchive_info(filepath):
    PYINST_COOKIE_SIZE = 24 + 64        # For pyinstaller 2.1+
    fp = open(filepath, 'rb')
    size = os.stat(filepath).st_size

    fp.seek(size - PYINST_COOKIE_SIZE, os.SEEK_SET)

    # Read CArchive cookie
    magic, lengthofPackage, toc, tocLen, pyver, pylibname = \
        struct.unpack('!8siiii64s', fp.read(PYINST_COOKIE_SIZE))
    fp.close()

    # Overlay is the data appended at the end of the PE
    pos = size - lengthofPackage
    return pos, pylibname.decode()


def repack_pyz(pyz, items, cipher=None):
    logger.info('Patching PYZ file "%s"', pyz)
    arch = ZlibArchive(pyz)
    updated = 0

    def compile_code(name, pyfile):
        logger.info('Compile %s', pyfile)
        with open(pyfile, 'r') as f:
            return compile(f.read(), '<%s>' % name, 'exec')

    code_dict = {}
    logic_toc = []

    for name in arch.toc:
        logger.debug('Extract %s', name)
        typ, obj = arch.extract(name)
        if name in items:
            logger.info('Update item "%s"', name)
            code_dict[name] = compile_code(name, items[name])
            items.pop(name)
            updated += 1
        else:
            code_dict[name] = obj
        pathname = '__init__.py' if typ == PYZ_TYPE_PKG else name
        logic_toc.append((name, pathname, 'PYMODULE'))

    ZlibArchiveWriter(pyz, logic_toc, code_dict=code_dict, cipher=cipher)
    logger.info('Patch PYZ done')

    return updated


def repack_exe(path, output, logic_toc):
    logger.info('Repacking EXE "%s"', output)

    offset, pylib_name = get_carchive_info(output)
    logger.info('Get archive info (%d, "%s")', offset, pylib_name)

    pkgname = os.path.join(path, 'PKG-patched')
    logging.info('Patch PKG file "%s"', pkgname)
    CArchiveWriter2(pkgname, logic_toc, pylib_name=pylib_name)

    if is_linux:
        logger.info('Update section pydata in EXE')
        Popen(['objcopy', '--update-section', 'pydata=%s' % pkgname, output])
    else:
        logger.info('Update patched PKG in EXE')
        with open(output, 'r+b') as outf:
            # Keep bootloader
            outf.seek(offset, os.SEEK_SET)

            # write the patched archive
            with open(pkgname, 'rb') as infh:
                shutil.copyfileobj(infh, outf, length=64*1024)

            outf.truncate()

    if is_darwin:
        # Fix Mach-O header for codesigning on OS X.
        logger.info('Fixing EXE for code signing "%s"', output)
        import PyInstaller.utils.osx as osxutils
        osxutils.fix_exe_for_code_signing(output)

    logger.info('Generate patched bundle "%s" successfully', output)


def repacker(executable, items):
    logger.info('Repack PyInstaller bundle "%s"', executable)

    name, ext = os.path.splitext(os.path.basename(executable))
    arch = CArchiveReader(executable)
    logic_toc = []

    path = os.path.join(name + '_extracted')
    logger.info('Extracted bundle files to "%s"', path)
    makedirs(path, exist_ok=True)

    for toc in arch.toc:
        logger.debug('toc: %s', toc)
        dpos, dlen, ulen, flag, typcd, nm = toc
        pathnm = os.path.join(path, nm)
        makedirs(os.path.dirname(pathnm), exist_ok=True)
        with arch.lib:
            arch.lib.seek(arch.pkg_start + dpos)
            with open(pathnm, 'wb') as f:
                f.write(arch.lib.read(dlen))

            if nm.endswith('.pyz') and typcd in ('z', 'Z'):
                logger.info('Extract pyz file "%s"', pathnm)
                patched = repack_pyz(pathnm, items)
            elif nm in items:
                patched = 1
                pathnm = items[nm]
            else:
                patched = 0
            logic_toc.append((patched, dlen, ulen, flag, typcd, nm, pathnm))

    output = os.path.join(name + '-patched' + ext)
    logger.info('Copy "%s" to "%s"', executable, output)
    shutil.copy2(executable, output)

    repack_exe(path, output, logic_toc)


def print_archive_items(executable):
    logger.info('PyInstaller bundle is "%s"', executable)

    path = os.path.join(os.path.basename(executable) + '_extracted')
    logger.info('Extracted bundle files to "%s"', path)
    if not os.path.exists(path):
        os.makedirs(path)

    logger.info('Got items from PKG')
    arch = CArchiveReader(executable)
    pyzlist = []
    for toc in arch.toc:
        logger.debug('toc: %s', toc)
        dpos, dlen, ulen, flag, typcd, nm = toc
        if nm.endswith('.pyz') and typcd in ('z', 'Z'):
            pathnm = os.path.join(path, nm)
            with arch.lib:
                arch.lib.seek(arch.pkg_start + dpos)
                with open(pathnm, 'wb') as f:
                    f.write(arch.lib.read(dlen))
            pyzlist.append(pathnm)
        else:
            logger.info('    %s (%d)', nm, ulen)

    for pyz in pyzlist:
        logger.info('Got items from "%s"', pyz)
        arch = ZlibArchive(pyz)
        for name in arch.toc:
            logger.info('    %s', name)


def build_updated_items(files, strip=None):
    items = {}
    for filename in files:
        if filename.find(os.pathsep) > 0:
            name, pathnm = filename.split(os.pathsep, 1)
            pathnm = os.path.normpath(filename)
        else:
            pathnm = os.path.normpath(filename)
            namelist = pathnm.split(os.sep)
            if strip is None:
                name = namelist[-2 if pathnm.endswith('__init__.py') else -1]
            else:
                name = '.'.join(namelist[strip:])
            for suffix in ('.py', '__init__.py'):
                if name.endswith(suffix):
                    name = name[:-len(suffix)].strip('.')
        items[name] = pathnm


def excepthook(type, value, traceback):
    if hasattr(value, 'args') and isinstance(str, value.args[0]):
        logger.error(*value.args)
    else:
        logger.error('%s', value)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug',
                        default=False,
                        action='store_true',
                        dest='debug',
                        help='print debug log (default: %(default)s)')
    parser.add_argument('-s', '--strip',
                        type=int, default=0, metavar='N',
                        help='strip the first Nth path from filename')
    parser.add_argument('executable', metavar='executable',
                        help="PyInstaller archive")
    parser.add_argument('files',
                        nargs='?',
                        help='updated files')

    args = parser.parse_args(sys.argv[1:])
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        sys.excepthook = excepthook

    if args.files is None:
        print_archive_items(args.executable)
        return

    items = build_updated_items(args.files, strip=args.strip)
    logging.info('Being updated items %s', items.keys())
    repacker(args.executable, items)


if __name__ == '__main__':
    main()
