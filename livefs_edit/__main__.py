#!/usr/bin/python3

import os
import sys

import yaml

from livefs_edit import cli
from livefs_edit.context import EditContext
from livefs_edit.actions import ACTIONS


HELP_TXT = """\
# livefs-edit source_path dest_path [actions]

livefs-edit makes modifications to Ubuntu live ISOs.
Normally source_path and dest_path should be paths to the .iso files or,
(experimental) already mounted directories/mountpoints

Actions include:
"""


def main(argv):
    if '--help' in argv:
        print(HELP_TXT)
        for action in sorted(ACTIONS.keys()):
            print(f" * --{action.replace('_', '-')}")
        print()
        sys.exit(0)

    isopath = argv[0]
    destpath = argv[1]

    already_mounted = False
    if os.path.isdir(isopath):
        already_mounted = True

    inplace = False
    if destpath == '/dev/null':
        destpath = None
    elif destpath == isopath and not already_mounted:
        destpath = destpath + '.new'
        inplace = True

    ctxt = EditContext(isopath)
    ctxt.mount_iso(already_mounted)

    if argv[2] == '--action-yaml':
        calls = []
        with open(argv[3]) as fp:
            spec = yaml.load(fp)
        print(spec)
        for action in spec:
            func = ACTIONS[action.pop('name')]
            calls.append((func, action))
    else:
        try:
            calls = cli.parse(ACTIONS, argv[2:])
        except cli.ArgException as e:
            print("parsing actions from command line failed:", e)
            sys.exit(1)

    try:
        for func, kw in calls:
            func(ctxt, **kw)

        if destpath is not None:
            if os.path.isdir(destpath):
                ctxt.repack_in_mounted(destpath)
            else:
                ctxt.repack_iso(destpath)
                if inplace:
                    os.rename(destpath, isopath)
    finally:
        ctxt.teardown()


if __name__ == '__main__':
    main(sys.argv[1:])
