import json
import yaml
import requests
import hashlib
import datetime
import shutil
from pathlib import Path
from git import Repo, RemoteProgress
from tqdm import tqdm
from packaging import version
from packaging.version import Version, InvalidVersion

class GitProgress(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        if max_count:
            print(f"\r{op_code} {cur_count}/{max_count} {message}", end='', flush=True)
        else:
            print(f"\r{op_code} {cur_count} {message}", end='', flush=True)

def parse_version_safe(v):
    """Parse version string, handling non-PEP 440 versions like '1.2.40.592'."""
    try:
        return version.parse(v)
    except InvalidVersion:
        # Fallback: convert to a dummy Version object
        parts = []
        for part in v.split('.'):
            try:
                parts.append(int(part))
            except ValueError:
                break
        # Pad to at least 3 parts
        while len(parts) < 3:
            parts.append(0)
        fallback_str = '.'.join(str(p) for p in parts)
        return Version(fallback_str)

def load_config_and_state():
    """Load and return config and state from files, or None if not found."""
    config_path = Path('config.json')
    state_path = Path('state.json')

    if not config_path.exists():
        print("config.json not found. Run 'invoke init --path=<path>' first.")
        return None, None

    if not state_path.exists():
        print("state.json not found. Run 'invoke init --path=<path>' first.")
        return None, None

    with open(config_path) as f:
        config = json.load(f)

    with open(state_path) as f:
        state = json.load(f)

    return config, state

def get_matching_publishers(mirror_dir, publisher):
    """Return list of publishers matching the filter string."""
    first_letter = publisher[0].lower()
    manifests_dir = Path(mirror_dir) / 'manifests'
    matching = []
    publisher_dir = manifests_dir / first_letter

    if publisher_dir.exists():
        for pub in publisher_dir.iterdir():
            if pub.is_dir() and pub.name.lower().startswith(publisher.lower()):
                matching.append(pub.name)

    return matching

def process_package(package_id, mirror_dir, downloads_dir, downloaded, repo, version_filter=None):
    """Process a single package: find version (latest or explicit), download if needed, update state."""
    try:
        pub, pkg = package_id.split('.', 1)
    except ValueError:
        print(f"Warning: Invalid package_id format: {package_id}")
        return False

    manifests_dir = mirror_dir / 'manifests'
    first_letter = pub[0].lower()
    publisher_path = manifests_dir / first_letter / pub
    package_path = publisher_path / pkg

    if not package_path.is_dir():
        print(f"Warning: Package directory not found for {package_id}")
        return False

    versions = [p.name for p in package_path.iterdir() if p.is_dir()]
    valid_versions = []
    for v in versions:
        try:
            parse_version_safe(v)
            valid_versions.append(v)
        except:
            continue

    if not valid_versions:
        return False

    # Explicit version support
    if version_filter:
        if version_filter not in valid_versions:
            print(f"Requested version {version_filter} not found for {package_id}")
            return False
        target_version = version_filter
    else:
        target_version = max(valid_versions, key=parse_version_safe)

    yaml_path = package_path / target_version / f'{pub}.{pkg}.yaml'
    if not yaml_path.exists():
        return False

    with open(yaml_path) as f:
        manifest = yaml.safe_load(f)

    installer_yaml_path = package_path / target_version / f'{pub}.{pkg}.installer.yaml'
    if installer_yaml_path.exists():
        with open(installer_yaml_path) as f:
            installer_manifest = yaml.safe_load(f)
        installers = installer_manifest.get('Installers', [])
    else:
        installers = manifest.get('Installers', [])

    download_dir = downloads_dir / pub / pkg / target_version
    download_dir.mkdir(parents=True, exist_ok=True)

    if package_id not in downloaded:
        downloaded[package_id] = {
            'versions': {}
        }

    downloaded[package_id]['versions'][target_version] = {
        'git_rev': repo.head.commit.hexsha,
        'files': {},
        'timestamp': datetime.datetime.now().isoformat(),
        'pinned': bool(version_filter)  # True if user passed --version
    }

    downloaded_new = False

    # Select installer: prefer x64, fallback to x86
    chosen_installer = None
    for inst in installers:
        if inst.get("Architecture") == "x64" and inst.get("InstallerUrl"):
            chosen_installer = inst
            break
    if not chosen_installer:
        for inst in installers:
            if inst.get("Architecture") == "x86" and inst.get("InstallerUrl"):
                chosen_installer = inst
                break

    if not chosen_installer:
        print(f"Skipping {package_id} — no x64/x86 installer with URL")
        return False

    # Proceed with download
    url = chosen_installer["InstallerUrl"]
    sha256 = chosen_installer.get("InstallerSha256")
    filename = Path(url).name
    filepath = download_dir / filename

    if filepath.exists():
        if filename not in downloaded[package_id]['versions'][target_version]['files']:
            with open(filepath, 'rb') as f:
                computed_hash = hashlib.sha256(f.read()).hexdigest()
            downloaded[package_id]['versions'][target_version]['files'][filename] = computed_hash

    else:
        downloaded_new = True
        print(f"Downloading {url} to {filepath}")
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"Skipping {package_id} — HTTP error: {e}")
            return False
        except requests.exceptions.RequestException as e:
            print(f"Skipping {package_id} — Request failed: {e}")
            return False

        total_size = int(response.headers.get('content-length', 0))

        with open(filepath, 'wb') as f, tqdm(
            desc=filename,
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        ) as bar:
            for data in response.iter_content(chunk_size=1024):
                size = f.write(data)
                bar.update(size)

        with open(filepath, 'rb') as f:
            computed_hash = hashlib.sha256(f.read()).hexdigest()

        if sha256 and computed_hash != sha256.lower():
            print(f"Warning: Hash mismatch for {filepath}, expected {sha256}, got {computed_hash}")

        downloaded[package_id]['versions'][target_version]['files'][filename] = computed_hash




    # Set timestamp after processing all installers
    if downloaded[package_id]['versions'][target_version]['files']:
        downloaded[package_id]['timestamp'] = datetime.datetime.now().isoformat()
        if not downloaded_new:
            print(f"Package {package_id} is already up to date")
        return True
    return False

class WingetMirrorManager:
    DEFAULT_CONFIG = {
        "repo_url": "https://github.com/microsoft/winget-pkgs",
        "revision": "master",
        "mirror_dir": "mirror",
        "patch_dir": "patched-manifests",
        "server_url": "https://localhost/winget",
        "cleanup": {
            "max_unpinned_versions": 5,
            "max_unpinned_age_months": 6
        }
    }

    def __init__(self, config_path='config.json', state_path='state.json'):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)

        if not self.config_path.exists():
            raise ValueError(f"Config file not found: {self.config_path}")

        if not self.state_path.exists():
            raise ValueError(f"State file not found: {self.state_path}")

        with open(self.config_path) as f:
            self.config = json.load(f)

        with open(self.state_path) as f:
            self.state = json.load(f)

        self.path = Path(self.state['path'])
        self.mirror_dir = self.path / self.config['mirror_dir']
        self.downloads_dir = self.path / 'downloads'
        self.repo = Repo(self.mirror_dir) if self.mirror_dir.exists() else None

    @classmethod
    def initialize(cls, path):
        """Initialize a new mirror usage at the specified path.

        Creates the project directory, config.json, and state.json if they don't exist.
        If already initialized at the path, does nothing.

        Args:
            path: Absolute or relative path to the project directory.

        Returns:
            str: Path to the initialized project directory.
        """
        project_path = Path(path)
        if not project_path.is_absolute():
            project_path = project_path.resolve()

        project_path.mkdir(parents=True, exist_ok=True)

        config_path = project_path / 'config.json'
        state_path = project_path / 'state.json'

        if config_path.exists():
            print(f"Already initialized at {project_path}")
            return str(project_path)

        config = cls.DEFAULT_CONFIG.copy()
        state = {
            "path": str(project_path),
            "last_sync": None
        }

        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)

        with open(state_path, 'w') as f:
            json.dump(state, f, indent=4)

        print(f"Initialized mirror at {project_path}")
        print(f"Config: {config_path}")
        print(f"State: {state_path}")

        return str(project_path)

    def paths(self):
        return {
            'path': self.path,
            'mirror_dir': self.mirror_dir,
            'downloads_dir': self.downloads_dir
        }

    def save_state(self):
        with open(self.path / 'state.json', 'w') as f:
            json.dump(self.state, f, indent=4)

    def get_matching_publishers(self, publisher):
        return get_matching_publishers(str(self.mirror_dir), publisher)

    def get_package(self, package_id):
        return WingetPackage(self, package_id)

    def sync_repo(self):
        """Sync the winget-pkgs git repository to the configured revision."""
        repo_path = self.mirror_dir
        if repo_path.exists():
            print("Updating repository...")
            repo = Repo(repo_path)
            # Ensure sparse checkout is configured
            try:
                sparse_enabled = repo.git.config('--get', 'core.sparseCheckout').strip()
            except:
                sparse_enabled = None
            if not sparse_enabled:
                repo.git.config('core.sparseCheckout', 'true')
                sparse_checkout_file = repo_path / '.git' / 'info' / 'sparse-checkout'
                sparse_checkout_file.parent.mkdir(parents=True, exist_ok=True)
                with open(sparse_checkout_file, 'w') as f:
                    f.write('manifests/\n')
                # Re-checkout to apply sparse checkout
                repo.git.checkout(self.config['revision'])
            else:
                repo.remotes.origin.fetch(progress=GitProgress())
                repo.git.checkout(self.config['revision'])
        else:
            print("Warning: Initial clone may take several minutes depending on your internet connection.")
            print("Cloning repository with sparse checkout...")
            repo = Repo.clone_from(self.config['repo_url'], repo_path, no_checkout=True, progress=GitProgress())
            # Set up sparse checkout
            repo.git.config('core.sparseCheckout', 'true')
            sparse_checkout_file = repo_path / '.git' / 'info' / 'sparse-checkout'
            sparse_checkout_file.parent.mkdir(parents=True, exist_ok=True)
            with open(sparse_checkout_file, 'w') as f:
                f.write('manifests/\n')
            # Checkout with sparse
            repo.git.checkout(self.config['revision'])

        print(f"Synced repo to {self.config['revision']} at {repo_path}")
        self.repo = repo
        return repo

    def patch_repo(self, server_url=None, patch_dir=None):
        """Create patched manifests with corrected InstallerURL paths.

        Uses server_url and patch_dir from config.json if not provided.
        """
        if not self.state.get("downloads"):
            print("No downloaded packages found in state.json")
            return 0

        # Resolve config defaults
        server_url = server_url or self.config.get("server_url")
        patch_dir = patch_dir or self.config.get("patch_dir")
        if not server_url or not patch_dir:
            print("Error: server_url and patch_dir must be set in config or passed explicitly")
            return 0

        output_path = Path(patch_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        patched_count = 0

        for package_id, package_info in self.state["downloads"].items():
            pub, pkg = package_id.split(".", 1)
            versions = package_info.get("versions", {})

            for version, vdata in versions.items():
                first_letter = pub[0].lower()
                source_manifest_dir = (
                    self.mirror_dir / "manifests" / first_letter / pub / pkg / version
                )
                if not source_manifest_dir.exists():
                    print(f"Warning: Source manifest not found for {package_id} {version}")
                    continue

                target_manifest_dir = (
                    output_path / "manifests" / first_letter / pub / pkg / version
                )
                target_manifest_dir.mkdir(parents=True, exist_ok=True)

                for manifest_file in source_manifest_dir.glob("*.yaml"):
                    target_file = target_manifest_dir / manifest_file.name
                    with open(manifest_file) as f:
                        manifest = yaml.safe_load(f)

                    if manifest.get("ManifestType") == "installer" and "Installers" in manifest:
                        for installer in manifest["Installers"]:
                            if "InstallerUrl" in installer:
                                original_url = installer["InstallerUrl"]
                                filename = Path(original_url).name
                                new_url = f"{server_url.rstrip('/')}/downloads/{pub}/{pkg}/{version}/{filename}"
                                installer["InstallerUrl"] = new_url
                                print(f"Patched {package_id} {version}: {original_url} -> {new_url}")

                    with open(target_file, "w") as f:
                        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

                patched_count += 1
                print(f"Patched manifests for {package_id} {version}")

        print(f"Successfully patched {patched_count} package versions")
        return patched_count

class WingetPackage:
    def __init__(self, manager, package_id):
        self.manager = manager
        self.package_id = package_id
        self.pub, self.pkg = package_id.split('.', 1)

    def get_latest_version(self):
        """Get the latest version of this package from the repository."""
        manifests_dir = self.manager.mirror_dir / 'manifests'
        first_letter = self.pub[0].lower()
        publisher_path = manifests_dir / first_letter / self.pub
        package_path = publisher_path / self.pkg

        if not package_path.is_dir():
            return None

        versions = [p.name for p in package_path.iterdir() if p.is_dir()]
        valid_versions = []
        for v in versions:
            try:
                parse_version_safe(v)
                valid_versions.append(v)
            except:
                continue

        if not valid_versions:
            return None

        return max(valid_versions, key=parse_version_safe)

    def download(self, version=None):
        """Download the latest version of this package."""
        downloaded = self.manager.state.setdefault('downloads', {})
        return process_package(self.package_id, self.manager.mirror_dir, self.manager.downloads_dir, downloaded, self.manager.repo, version_filter=version)

    def validate_hashes(self):
        """Validate SHA256 hashes of downloaded files for this package."""
        package_info = self.manager.state.get('downloads', {}).get(self.package_id)
        if not package_info:
            return {"valid": False, "error": "Package not in state"}

        results = {"valid": True, "versions": {}}

        for version, vdata in package_info.get("versions", {}).items():
            expected_files = vdata.get("files", {})
            version_results = {
                "valid": True,
                "files": {},
                "missing_files": [],
                "unexpected_files": []
            }

            if not expected_files:
                results["versions"][version] = version_results
                continue

            download_dir = self.manager.downloads_dir / self.pub / self.pkg / version
            if not download_dir.exists():
                version_results["valid"] = False
                results["versions"][version] = version_results
                results["valid"] = False
                continue

            actual_files = {f.name: f for f in download_dir.iterdir() if f.is_file()}

            for filename, expected_hash in expected_files.items():
                if filename not in actual_files:
                    version_results["missing_files"].append(filename)
                    version_results["valid"] = False
                    continue

                filepath = actual_files[filename]
                with open(filepath, 'rb') as f:
                    computed_hash = hashlib.sha256(f.read()).hexdigest()

                match = computed_hash == expected_hash
                status = "MATCH" if match else "MISMATCH"
                version_results["files"][filename] = {
                    "status": status,
                    "expected": expected_hash,
                    "computed": computed_hash
                }
                if not match:
                    version_results["valid"] = False

            expected_filenames = set(expected_files.keys())
            actual_filenames = set(actual_files.keys())
            unexpected = actual_filenames - expected_filenames
            if unexpected:
                version_results["unexpected_files"] = list(unexpected)

            results["versions"][version] = version_results
            if not version_results["valid"]:
                results["valid"] = False

        return results

    def purge(self, version=None):
        """Purge downloaded files, state, and patched manifests for this package.

        If version is given, purge only that version.
        Otherwise purge all versions.
        """
        package_info = self.manager.state.get("downloads", {}).get(self.package_id)
        if not package_info:
            return False

        versions = package_info.get("versions", {})
        if not versions:
            return False

        purged_any = False
        for v in list(versions.keys()):
            if version and v != version:
                continue

            # Remove downloads
            package_dir = self.manager.downloads_dir / self.pub / self.pkg / v
            if package_dir.exists():
                shutil.rmtree(package_dir)

            # Remove patched manifests
            patched_root = Path(self.manager.config.get("patched_dir", "patched-manifests"))
            patched_dir = patched_root / "manifests" / self.pub[0].lower() / self.pub / self.pkg / v
            if patched_dir.exists():
                shutil.rmtree(patched_dir)
                print(f"Removed patched manifests for {self.package_id} {v}")

            # Remove from state
            del versions[v]
            purged_any = True
            print(f"Purged {self.package_id} {v}")

        # If no versions left, remove package entry entirely
        if not versions:
            del self.manager.state["downloads"][self.package_id]

        if purged_any:
            self.manager.save_state()
        return purged_any

    def get_status(self):
        """Get the status of this package."""
        downloaded_packages = self.manager.state.get('downloads', {})
        if self.package_id in downloaded_packages:
            package_info = downloaded_packages[self.package_id]
            files = package_info.get('files', {})
            version = package_info.get('version', 'unknown')
            timestamp = package_info.get('timestamp', 'unknown')
            if timestamp != 'unknown' and timestamp != '-':
                try:
                    dt = datetime.datetime.fromisoformat(timestamp)
                    timestamp = dt.strftime('%Y-%m-%d %H:%M')
                except:
                    pass
            if files:
                download_dir = self.manager.downloads_dir / self.pub / self.pkg / version
                if download_dir.exists():
                    actual_files = list(download_dir.glob('*'))
                    if actual_files:
                        status = "Downloaded"
                    else:
                        status = "Downloaded (empty)"
                        version = "-"
                        timestamp = "-"
                else:
                    status = "Downloaded (missing)"
                    version = "-"
                    timestamp = "-"
            else:
                status = "Recorded"
                version = version
                timestamp = "-"
        else:
            status = "Not downloaded"
            version = "-"
            timestamp = "-"

        return {
            'status': status,
            'version': version,
            'timestamp': timestamp
        }