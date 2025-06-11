#!/usr/bin/env python
# Copyright (C) 2021 Wes Barnett

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""Script for taking pre/post snapshots; run from pacman hooks."""

from argparse import ArgumentParser
from configparser import ConfigParser
import json
import logging
from pathlib import Path
import os
import sys
import tempfile
import subprocess


logging.basicConfig(format="%(message)s", level=logging.INFO)


class SnapperCmd:

    def __init__(self, config, snapshot_type, cleanup_algorithm, description="",
                 nodbus=False, pre_number=None, userdata=""):
        self.cmd = ["snapper"]
        if nodbus:
            self.cmd.append("--no-dbus")
        self.cmd.extend([
            f"--config {config} create",
            f"--cleanup-algorithm {cleanup_algorithm}",
            "--print-number"
        ])
        if description:
            self.cmd.append(f"--description \"{description}\"")
        if userdata:
            self.cmd.append(f"--userdata \"{userdata}\"")
        if snapshot_type == "post":
            if pre_number is not None:
                self.cmd.append(f"--pre-number {pre_number}")
            else:
                logging.debug("snapshot type specified as 'post' but no pre snapshot number, "
                              "so setting snapshot type to 'single'. If installing "
                              "snap-pac this is normal.")
                snapshot_type = "single"
        self.cmd.append(f"--type {snapshot_type}")

    def __call__(self):
        return os.popen(self.__str__()).read().rstrip("\n")

    def __str__(self):
        return " ".join(self.cmd)


class ConfigProcessor:

    def __init__(self, ini_file, snapshot_type, parent_cmd=None, packages=None):
        """Set up defaults for snap-pac configuration."""

        if parent_cmd is None:
            self.parent_cmd = os.popen(f"ps -p {os.getppid()} -o args=").read().strip()
        else:
            self.parent_cmd = parent_cmd

        if packages is None:
            self.packages = [line.rstrip("\n") for line in sys.stdin]
        else:
            self.packages = packages

        self.snapshot_type = snapshot_type

        self.config = ConfigParser()
        self.config["DEFAULT"] = {
            "snapshot": False,
            "cleanup_algorithm": "number",
            "pre_description": self.parent_cmd,
            "post_description": " ".join(self.packages),
            "desc_limit": 72,
            "important_packages": [],
            "important_commands": [],
            "userdata": []
        }
        self.config["root"] = {
            "snapshot": True
        }
        self.config.read(ini_file)

    def get_cleanup_algorithm(self, section):
        return self.config.get(section, "cleanup_algorithm")

    def get_description(self, section):
        desc_limit = self.config.getint(section, "desc_limit")
        return self.config.get(section, f"{self.snapshot_type}_description")[:desc_limit]

    def check_important_commands(self, section):
        return self.parent_cmd in json.loads(self.config.get(section, "important_commands"))

    def check_important_packages(self, section):
        important_packages = json.loads(self.config.get(section, "important_packages"))
        return any(x in important_packages for x in self.packages)

    def check_important(self, section):
        return (self.check_important_commands(section) or
                self.check_important_packages(section))

    def get_userdata(self, section):
        userdata = set(json.loads(self.config.get(section, "userdata")))
        if self.check_important(section):
            userdata.add("important=yes")
        return ",".join(sorted(list(userdata)))

    def __call__(self, section):
        if section not in self.config:
            self.config.add_section(section)
        return {
            "description": self.get_description(section),
            "cleanup_algorithm": self.get_cleanup_algorithm(section),
            "userdata": self.get_userdata(section),
            "snapshot": self.config.getboolean(section, "snapshot")
        }


def get_snapper_configs(conf_file):
    """Get the snapper configurations."""
    for line in conf_file.read_text().split("\n"):
        if line.startswith("SNAPPER_CONFIGS"):
            line = line.rstrip("\n").rstrip("\"").split("=")
            return line[1].lstrip("\"").split()


class Prefile:
    """Handles reading and writing of pre snapshot number."""
    def __init__(self, snapper_config, snapshot_type):
        self.file = Path(tempfile.gettempdir()) / f"snap-pac-pre_{snapper_config}"
        self.snapshot_type = snapshot_type

    def read(self):
        if self.snapshot_type == "pre":
            pre_number = None
        else:
            try:
                pre_number = self.file.read_text().rstrip("\n")
            except FileNotFoundError:
                pre_number = None
                logging.debug(f"prefile {self.file} not found. Ensure you have run the pre snapshot first. "
                              "If installing snap-pac this is normal.")
            else:
                self.file.unlink()
        return pre_number

    def write(self, num):
        if self.snapshot_type == "pre":
            self.file.write_text(num)


def check_skip():
    return os.getenv("SNAP_PAC_SKIP", "n").lower() in ["y", "yes", "true", "1"]


def parse_args():
    parser = ArgumentParser(description="Script for taking pre/post snapper snapshots. Used with pacman hooks.")
    parser.add_argument(dest="type", choices=["pre", "post"], help="snapper snapshot type")
    parser.add_argument(
        "--ini", dest="snap_pac_ini", type=Path,
        default=Path("/etc/snap-pac.ini"), help="snap-pac ini file path"
    )
    parser.add_argument(
        "--conf", dest="snapper_conf_file", type=Path,
        default=Path("/etc/conf.d/snapper"), help="snapper configuration file path"
    )
    return parser.parse_args()


if __name__ == "__main__":

    if check_skip():
        logging.warning("snapper snapshots skipped")
        quit()

    args = parse_args()
    config_processor = ConfigProcessor(args.snap_pac_ini, args.type)
    chroot = os.stat("/") != os.stat("/proc/1/root/.")

    for snapper_config in get_snapper_configs(args.snapper_conf_file):

        data = config_processor(snapper_config)
        if data["snapshot"]:
            if os.path.isfile("/var/lib/pacman/db.lck"):
                subprocess.run("mkdir -p /tmp/snap-pac", shell=True, check=True)
                subprocess.run("mv /var/lib/pacman/db.lck /tmp/snap-pac/ && sync", shell=True, check=True)
            prefile = Prefile(snapper_config, args.type)
            pre_number = prefile.read()
            num = SnapperCmd(snapper_config, args.type, data["cleanup_algorithm"],
                             data["description"], chroot, pre_number, data["userdata"])()
            logging.info(f"==> {snapper_config}: {num}")
            prefile.write(num)
            if os.path.isfile("/tmp/snap-pac/db.lck"):
                subprocess.run("mv /tmp/snap-pac/db.lck /var/lib/pacman/ && sync", shell=True, check=True)
