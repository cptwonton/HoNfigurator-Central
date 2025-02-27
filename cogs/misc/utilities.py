import subprocess, psutil
import os
import hashlib
import crcmod
import zipfile
import binascii
from os.path import exists
from pathlib import Path
import sys
from cpuinfo import get_cpu_info
import requests
import aiohttp
from cogs.misc.logger import get_logger, get_home
from cogs.misc.exceptions import HoNUnexpectedVersionError, HoNCompatibilityError
import ipaddress
import asyncio
import schedule
import time


LOGGER = get_logger()
HOME_PATH = get_home()

class Misc:
    def __init__(self):
        self.cpu_count = psutil.cpu_count(logical=True)
        self.cpu_name = get_cpu_info().get('brand_raw', 'Unknown CPU')
        self.total_ram = psutil.virtual_memory().total
        self.os_platform = sys.platform
        self.total_allowed_servers = None
        self.github_branch_all = self.get_all_branch_names()
        self.github_branch = self.get_current_branch_name()
        self.public_ip = self.lookup_public_ip()
        self.tag = None
        self.tag = self.get_github_tag()
        schedule.every(10).minutes.do(self.check_github_tag)
        self.hon_version = None
        self.used_space = 0
        self.total_space = 0
        self.usage_percentage = 0

    def build_commandline_args(self, config_local, config_global, cowmaster=False):

        # remove host_affinity if override is enabled, which is used by the game to manage it's affinity. Instead, lets the code handle affinity assignment
        if self.get_os_platform() == "windows" and config_local['params']['svr_override_affinity']:
            config_local['params'].pop('host_affinity')
        # Prepare the parameters
        params = ';'.join(' '.join((f"Set {key}", str(val))) for (key, val) in config_local['params'].items())

        # Base command
        base_cmd = [
            config_local['config']['file_path'],
            "-dedicated",
            "-noconfig",
            "-execute",
            params if self.get_os_platform() == "win32" else f'"{params}"',
            "-masterserver",
            config_global['hon_data']['svr_masterServer'],
            "-register",
            f"127.0.0.1:{config_global['hon_data']['svr_managerPort']}"
        ]

        # Additional options based on conditions
        if self.get_os_platform() == "win32":
            base_cmd.insert(2,"-mod")
            base_cmd.insert(3,"game;KONGOR")

            if config_global['hon_data']['svr_noConsole']:
                base_cmd.insert(4, "-noconsole")
            
            
            if cowmaster:
                base_cmd.insert(1, '-cowmaster')
                base_cmd.insert(2, '-servicecvars')

        elif self.get_os_platform() == "linux":
            base_cmd.insert(2, "-mod game;KONGOR")  # Modify the mod parameter

            if cowmaster:
                base_cmd.insert(1, '-cowmaster')
                base_cmd.insert(2, '-servicecvars')
                base_cmd.insert(3, '-noconsole')

        return base_cmd

    def parse_linux_procs(self, proc_name, slave_id):
        for proc in psutil.process_iter():
            if proc_name == proc.name():
                if slave_id == '':
                    return [ proc ]
                else:
                    cmd_line = proc.cmdline()
                    if len(cmd_line) < 5:
                        continue
                    for i in range(len(cmd_line)):
                        if cmd_line[i] == "-execute":
                            for item in cmd_line[i+1].split(";"):
                                if "svr_slave" in item:
                                    if int(item.split(" ")[-1]) == slave_id:
                                        return [ proc ]
        return []

    def get_proc(self, proc_name, slave_id=''):
        if sys.platform == "linux":
            return self.parse_linux_procs(proc_name, slave_id)
        procs = []
        for proc in psutil.process_iter():
            try:
                if proc_name == proc.name():
                    if slave_id == '':
                        procs.append(proc)
                    else:
                        cmd_line = proc.cmdline()
                        if len(cmd_line) < 5:
                            continue
                        for i in range(len(cmd_line)):
                            if cmd_line[i] == "-execute":
                                for item in cmd_line[i+1].split(";"):
                                    if "svr_slave" in item:
                                        if int(item.split(" ")[-1]) == slave_id:
                                            procs.append(proc)
            except psutil.NoSuchProcess:
                pass
        return procs

    def get_process_by_port(self, port, protocol='udp4'):
        for connection in psutil.net_connections(kind=protocol):
            if connection.laddr.port == port:
                return psutil.Process(connection.pid)
        return None

    def get_client_pid_by_tcp_source_port(self, local_server_port, client_source_port):
        """
        Get the Process object of a local client based on its source port and the server port it's connecting to.
        """
        for connection in psutil.net_connections(kind='inet'):
            # Check for a match on both client source port and server port
            if connection.laddr.port == client_source_port and connection.raddr.port == local_server_port and connection.status == 'ESTABLISHED':
                return psutil.Process(connection.pid)
        return None

    def check_port(self, port):
        for conn in psutil.net_connections('udp'):
            if conn.laddr.port == port:
                return True
        return False

    def get_process_priority(proc_name):
        pid = False
        for proc in psutil.process_iter():
            if proc.name() == proc_name:
                pid = proc.pid
        if pid:
            p = next((proc for proc in psutil.process_iter() if proc.pid == pid),None)
            prio = p.nice()
            prio = (str(prio)).replace("Priority.","")
            prio = prio.replace("_PRIORITY_CLASS","")
            if prio == "64": prio = "IDLE"
            elif prio == "128": prio = "HIGH"
            elif prio == "256": prio = "REALTIME"
            return prio
        else: return "N/A"

    def get_cpu_count(self):
        return self.cpu_count

    def get_cpu_name(self):
        if self.get_os_platform() == "win32":
            return self.cpu_name
        elif self.get_os_platform() == "linux":
            # Linux uses the /proc/cpuinfo file
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('model name'):
                        return line.split(':')[1].strip()

    def format_memory(self,value):
        if value < 1:
            return round(value * 1024)  # Convert to MB and round
        else:
            return round(value, 2)  # Keep value in GB, round to 2 decimal points

    def get_total_ram(self):
        total_ram_gb = self.total_ram / (1024 ** 3)  # Convert bytes to GB
        return self.format_memory(total_ram_gb)

    def get_used_ram(self):
        used_ram_gb = psutil.virtual_memory().used / (1024 ** 3)  # Convert bytes to GB
        return self.format_memory(used_ram_gb)

    def get_cpu_load(self):
        # Getting loadover15 minutes
        load1, load5, load15 = psutil.getloadavg()

        cpu_usage = (load1/os.cpu_count()) * 100

        return round(cpu_usage, 2)

    def get_os_platform(self):
        return self.os_platform

    def get_num_reserved_cpus(self):
        if self.cpu_count <=4:
            return 1
        elif self.cpu_count >4 and self.cpu_count <= 12:
            return 2
        elif self.cpu_count >12:
            return 4

    def get_total_allowed_servers(self,svr_total_per_core):
        total = svr_total_per_core * self.cpu_count
        if self.cpu_count < 5:
            total -= 1
        elif self.cpu_count > 4 and self.cpu_count < 13:
            total -= 2
        elif self.cpu_count >12:
            total -= 4
        return int(total)

    def get_server_affinity(self, server_id, svr_total_per_core):
        server_id = int(server_id)
        affinity = []

        if svr_total_per_core > 3 or svr_total_per_core < 0.5 or (svr_total_per_core != 0.5 and svr_total_per_core != int(svr_total_per_core)):
            raise Exception("Value must be 0.5, 1, 2, or 3.")

        if svr_total_per_core == 0.5:
            cores_per_server = 2
            starting_core = (self.cpu_count) - (server_id * cores_per_server) % self.cpu_count
            for i in range(cores_per_server):
                affinity.append(str((starting_core + i) % self.cpu_count))
        elif svr_total_per_core == 1:
            affinity.append(str(self.cpu_count - server_id))
        else:
            t = 0
            for num in range(0, server_id):
                if num % svr_total_per_core == 0:
                    t += 1
            affinity.append(str(self.cpu_count - t))

        return affinity

    def get_public_ip(self):
        if self.public_ip:
            return self.public_ip
        return self.lookup_public_ip()

    def lookup_public_ip(self):
        try:
            self.public_ip = requests.get('https://4.ident.me').text
        except Exception:
            try:
                self.public_ip = requests.get('http://api.ipify.org').text
            except Exception:
                LOGGER.error("Failed to fetch public IP")
                self.public_ip = None
        return self.public_ip

    async def lookup_public_ip_async(self):
        providers = ['http://4.ident.me','https://4.ident.me', 'http://api.ipify.org/', 'https://api.ipify.org', 'https://ifconfig.me','https://myexternalip.com/raw','https://wtfismyip.com/text']
        timeout = aiohttp.ClientTimeout(total=5)  # Set the timeout for the request in seconds

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for provider in providers:
                try:
                    async with session.get(provider) as response:
                        if response.status == 200:
                            ip_str = await response.text()
                            try:
                                # Try to construct an IP address object. If it fails, this is not a valid IP.
                                ipaddress.ip_address(ip_str)
                                return ip_str
                            except ValueError:
                                LOGGER.warn(f"Invalid IP received from {provider}. Trying another provider...")
                except asyncio.TimeoutError:
                    LOGGER.warn(f"Timeout when trying to fetch IP from {provider}. Trying another provider...")
                    continue
                except Exception as e:
                    LOGGER.warn(f"Error occurred when trying to fetch IP from {provider}: {e}")
                    continue
            LOGGER.critical("Tried all public IP providers and could not determine public IP address. This will most likely cause issues.")

    def get_svr_description(self):
        return f"cpu: {self.get_cpu_name()}"

    def find_process_by_cmdline_keyword(self, keyword, proc_name=None):
        for process in psutil.process_iter(['cmdline']):
            if process.info['cmdline']:
                if keyword in process.info['cmdline']:
                    if proc_name:
                        if proc_name == process.name():
                            return process
                    else:
                        return process
        return None

    def get_svr_version(self,hon_exe):
        def validate_version_format(version):
            version_parts = version.split('.')
            if len(version_parts) != 4:
                return False

            for part in version_parts:
                try:
                    int(part)
                except ValueError:
                    return False

            return True

        if not exists(hon_exe):
            raise FileNotFoundError(f"File {hon_exe} does not exist.")

        if self.get_os_platform() == "win32":
            version_offset = 88544
            version_len = 18
        elif self.get_os_platform() == "linux":
            version_offset = 0x148b8
            version_len = 36
        else:
            raise HoNCompatibilityError("Unsupported OS")
        
        with open(hon_exe, 'rb') as hon_x64:
            hon_x64.seek(version_offset, 1)
            version = hon_x64.read(version_len)
            # Split the byte array on b'\x00' bytes
            split_bytes = version.split(b'\x00')
            # Decode the byte sequences and join them together
            version = ''.join(part.decode('utf-8') for part in split_bytes if part)
        
        LOGGER.debug(f"Detected version number: {version}")

        if not validate_version_format(version):
            raise HoNUnexpectedVersionError("Unexpected game version. Have you merged the wasserver binaries into the HoN install folder?")

        self.hon_version = version
        return version

    def update_github_repository(self):
        try:
            # Change the current working directory to the HOME_PATH
            os.chdir(HOME_PATH)

            # Run the git pull command
            LOGGER.debug("Checking for upstream HoNfigurator updates.")
            result = subprocess.run(["git", "pull"], text=True, capture_output=True)

            # Log any errors encountered
            if result.stderr and result.returncode != 0:
                LOGGER.error(f"Error encountered while updating: {result.stderr}")

                # If the error is due to divergent branches, reset the branch
                if "hint: You have divergent branches" in result.stderr:
                    current_branch = self.get_current_branch_name()
                    LOGGER.warning(f"Detected divergent branches. Resetting {current_branch} to match remote...")
                    reset_result = subprocess.run(["git", "reset", "--hard", f"origin/{current_branch}"], text=True, capture_output=True)

                    if reset_result.stderr:
                        LOGGER.error(f"Error resetting branch {current_branch}: {reset_result.stderr}")
                    else:
                        LOGGER.info(f"Successfully reset {current_branch} to match remote.")

            # Check if the update was successful
            if "Already up to date." not in result.stdout and "Fast-forward" in result.stdout:
                LOGGER.info("Update successful. Relaunching the code...")

                # Relaunch the code
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                LOGGER.debug("HoNfigurator already up to date. No need to relaunch.")
        except subprocess.CalledProcessError as e:
            LOGGER.error(f"Error updating the code: {e}")

    def calculate_crc32(self, file_path):
        crc32_func = crcmod.predefined.mkCrcFun('crc-32')
        crc = 0

        with open(file_path, 'rb') as file:
            for chunk in iter(lambda: file.read(4096), b''):
                crc = crc32_func(chunk, crc)

        return binascii.hexlify(crc.to_bytes(4, 'big')).decode()

    def calculate_md5(self, file_path):
        with open(file_path, "rb") as file:
            md5_hash = hashlib.md5()
            chunk_size = 4096

            while chunk := file.read(chunk_size):
                md5_hash.update(chunk)

            return md5_hash.hexdigest()

    def unzip_file(self, source_zip, dest_unzip):
        extracted_files = []
        with zipfile.ZipFile(source_zip, 'r') as zip_ref:
            extracted_files = zip_ref.namelist()  # Get the list of filenames
            zip_ref.extractall(dest_unzip)  # Extract all files to the destination directory

        return extracted_files

    def save_last_working_branch(self):
        with open(HOME_PATH / "logs" / ".last_working_branch", "w") as f:
            f.write(self.get_current_branch_name())

    def get_current_branch_name(self):
        try:
            os.chdir(HOME_PATH)
            branch_name = subprocess.check_output(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                universal_newlines=True
            ).strip()
            return branch_name
        except subprocess.CalledProcessError as e:
            LOGGER.error(f"{HOME_PATH} Not a git repository: {e.output}")
            return None
    
    def check_github_tag(self):
        # Assuming 'misc' is an instance of your Misc class
        new_tag = self.get_github_tag()
        LOGGER.debug(f"Checked GitHub Tag: {new_tag}")
    
    def get_github_tag(self):
        try:
            if self.tag:
                return self.tag
            tag = subprocess.check_output(['git', 'describe', '--tags'], stderr=subprocess.STDOUT).decode().strip()
            return tag.split('-')[0]
        except subprocess.CalledProcessError:
            LOGGER.error("Error: Failed to get the tag. Make sure you're in a Git repository and have tags.")
            return None
    
    def get_github_branch(self):
        if self.github_branch:
            return self.github_branch
        else:
            return self.get_current_branch_name()

    def get_git_commit_date(self):
        command = 'git log -1 --format="%cd" --date=format-local:"%Y-%m-%d %H:%M:%S"'
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.stdout.strip()

    def get_all_branch_names(self):
        try:
            os.chdir(HOME_PATH)
            # Fetch the latest information from the remote repository
            subprocess.run(['git', 'fetch'])

            # Remove any stale remote-tracking branches
            subprocess.run(['git', 'remote', 'prune', 'origin'])

            # Retrieve the branch names from the remote repository
            branch_names = subprocess.check_output(
                ['git', 'for-each-ref', '--format=%(refname:lstrip=3)', 'refs/remotes/origin/'],
                universal_newlines=True
            ).strip()
            branches = [branch.strip() for branch in branch_names.split('\n') if branch.strip() != 'HEAD']

            # Process the branch names, excluding "HEAD"
            return branches
        except subprocess.CalledProcessError as e:
            LOGGER.error(f"{HOME_PATH} Not a git repository: {e.output}")
            return None

    def change_branch(self, target_branch):
        try:
            os.chdir(HOME_PATH)
            current_branch = self.get_current_branch_name()

            if current_branch == target_branch:
                LOGGER.info(f"Already on target branch '{target_branch}'")
                return "Already on target branch"

            result = subprocess.run(['git', 'checkout', target_branch], text=True, capture_output=True)

            # Log any errors encountered
            if result.stderr and result.returncode != 0:
                LOGGER.error(f"Error encountered while switching branches: {result.stderr}")
                return result.stderr

            LOGGER.info(f"Switched to branch '{target_branch}'")
            LOGGER.info("Relaunching code into new branch.")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except subprocess.CalledProcessError as e:
            LOGGER.error(f"Error: Could not switch to branch '{target_branch}', make sure it exists: {e.output}")
    
    def get_disk_usage(self):
        if os.name == 'nt':  # Windows
            drive_path = 'C:\\'
        elif os.name == 'posix':  # Linux
            drive_path = '/'
        else:
            LOGGER.error("Unsupported operating system")
            # raise NotImplementedError("Unsupported operating system")

        try:
            disk_usage = psutil.disk_usage(drive_path)
            self.used_space = disk_usage.used
            self.total_space = disk_usage.total
            self.usage_percentage = disk_usage.percent
        except Exception as e:
            print(f"Error getting disk usage: {e}")

        return {
            'used_space': self.used_space,
            'total_space': self.total_space,
            'usage_percentage': self.usage_percentage
        }