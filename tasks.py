import json
import datetime
import sys
import os
import shutil
from invoke import task

# Add current directory to path for imports
sys.path.insert(0, os.path.join(os.getcwd(), '..'))

from winget_mirror_core import (
    parse_version_safe, WingetMirrorManager
)

# Check Python version
if sys.version_info < (3, 11):
    print("Error: This tool requires Python 3.11 or higher.")
    print(f"Current version: {sys.version}")
    sys.exit(1)

@task
def init(c, path):
    """Initialize a new mirror usage at the specified path.

    Creates the project directory, config.json, and state.json if they don't exist.
    If already initialized at the path, does nothing.

    Args:
        path: Absolute or relative path to the project directory.

    Example:
        invoke init --path="/path/to/mirror"
    """
    WingetMirrorManager.initialize(path)

@task
def sync(c, publisher, version=None):
    """Download the latest version of packages matching the publisher/package filter from the already synced repository.

    Downloads the latest version of packages matching the publisher/package filter.
    Optionally specify a version (e.g. --version 1.2.3).
    The repository must be synced first using 'invoke sync-repo'.

    Args:
        publisher: Publisher filter, optionally with package filter and --version

    Example:
        invoke sync Microsoft
        invoke sync Splunk/ACS
        invoke sync Spotify/Spotify --version 1.2.3
    """
    manager = WingetMirrorManager()
    if manager.repo is None:
        print("Repository not found. Run 'invoke sync-repo' first.")
        return

    processed_packages = set()

    # Parse publisher/package filter
    if "/" in publisher:
        pub_filter, pkg_filter = publisher.split("/", 1)
    else:
        pub_filter = publisher
        pkg_filter = None

    publishers = manager.get_matching_publishers(pub_filter)

    manifests_dir = manager.mirror_dir / 'manifests'

    for pub in publishers:
        first_letter = pub[0].lower()
        publisher_path = manifests_dir / first_letter / pub
        for package_path in publisher_path.iterdir():
            if not package_path.is_dir():
                continue

            # Filter by package name if specified
            if pkg_filter and not package_path.name.lower().startswith(pkg_filter.lower()):
                continue

            package_id = f'{pub}.{package_path.name}'
            pkg = manager.get_package(package_id)
            if pkg.download(version=version):   # pass version down
                processed_packages.add(package_id)

    # Update state
    manager.state['last_sync'] = datetime.datetime.now().isoformat()
    manager.save_state()

    if publisher:
        print(f"Downloaded {len(processed_packages)} packages matching '{publisher}'")

@task
def refresh_synced(c):
    """Refresh all synced packages to their latest versions.

    Checks each package in state.json for newer versions in the repository
    and downloads/updates them if available. Leaves pinned versions untouched.
    The repository must be synced first.
    """
    manager = WingetMirrorManager()
    if manager.repo is None:
        print("Repository not found. Run 'invoke sync-repo' first.")
        return

    updated_packages = set()

    for package_id, package_info in manager.state.get('downloads', {}).items():
        versions = package_info.get("versions", {})
        if not versions:
            continue

        # Find the latest non-pinned version we have
        non_pinned_versions = [
            v for v, vdata in versions.items() if not vdata.get("pinned")
        ]
        if not non_pinned_versions:
            print(f"{package_id} has only pinned versions, skipping refresh")
            continue

        current_version = max(non_pinned_versions, key=parse_version_safe)

        pkg = manager.get_package(package_id)
        latest_version = pkg.get_latest_version()

        if latest_version and parse_version_safe(latest_version) > parse_version_safe(current_version):
            print(f"Updating {package_id} from {current_version} to {latest_version}")
            if pkg.download(version=latest_version):
                updated_packages.add(package_id)
        else:
            print(f"{package_id} is up to date")

    # Update state
    manager.state['last_sync'] = datetime.datetime.now().isoformat()
    manager.save_state()

    print(f"Refreshed {len(updated_packages)} packages")


@task
def sync_repo(c):
    """Sync the winget-pkgs git repository to the configured revision.

    Clones the repository if it doesn't exist, pulls latest changes if it does,
    and checks out the configured revision.

    This task must be run before 'sync' to ensure the repository is up to date.

    Example:
        invoke sync-repo
    """
    manager = WingetMirrorManager()
    manager.sync_repo()

@task
def validate_hash(c, output=None):
    """Validate SHA256 hashes of all downloaded files against stored checksums.

    Checks that all expected files exist and their hashes match the recorded values.
    Exits with error code 1 if any validation fails.

    Args:
        output: Optional output format. Use 'json' for JSON output, otherwise human-readable text.

    Examples:
        invoke validate-hash
        invoke validate-hash --output=json
    """
    manager = WingetMirrorManager()

    if 'downloads' not in manager.state or not manager.state['downloads']:
        if output == 'json':
            print(json.dumps({"all_valid": True, "packages": {}}, indent=4))
        else:
            print("No downloaded packages found in state.json")
        return

    results = {
        "all_valid": True,
        "packages": {}
    }

    for package_id in manager.state['downloads']:
        pkg = manager.get_package(package_id)
        pkg_results = pkg.validate_hashes()
        results["packages"][package_id] = pkg_results
        if not pkg_results["valid"]:
            results["all_valid"] = False

    if output == 'json':
        print(json.dumps(results, indent=4))
    else:
        # Print human-readable output
        for package_id, pkg_data in results["packages"].items():
            for version, vdata in pkg_data.get("versions", {}).items():
                if not vdata["files"] and not vdata["missing_files"]:
                    print(f"Warning: No files recorded for {package_id} {version}")
                    continue

                if not vdata["valid"] and not vdata["files"] and vdata["missing_files"]:
                    publisher, package = package_id.split('.', 1)
                    download_dir = manager.downloads_dir / publisher / package / version
                    print(f"Error: Download directory missing for {package_id} {version}: {download_dir}")
                    continue

                for filename, file_data in vdata["files"].items():
                    status = file_data["status"]
                    print(f"Validating {package_id}/{version}/{filename}: {status}")
                    print(f"  Tracked hash: {file_data['expected']}")
                    print(f"  Computed hash: {file_data['computed']}")

                for missing in vdata["missing_files"]:
                    print(f"Error: Expected file missing for {package_id} {version}: {missing}")

                for unexpected in vdata["unexpected_files"]:
                    print(f"Warning: Unexpected files in {package_id} {version}: {unexpected}")

        if results["all_valid"]:
            print("All downloaded files validated successfully!")
        else:
            print("Validation failed! Some files are missing or corrupted.")
            sys.exit(1)


@task
def purge_package(c, target, version=None):
    """Purge downloaded packages.

    Args:
        target: Publisher filter (e.g., 'Microsoft'),
                or Publisher/Package (e.g., 'Microsoft/Teams')
        version: Optional version string (e.g., '1.2.3')

    Examples:
        invoke purge-package Microsoft
        invoke purge-package Microsoft/Teams
        invoke purge-package Microsoft/Teams --version=1.2.3
    """
    manager = WingetMirrorManager()

    if 'downloads' not in manager.state or not manager.state['downloads']:
        print("No downloaded packages found in state.json")
        return

    # Parse target
    if '/' in target:
        publisher, package = target.split('/', 1)
        matching_packages = [
            f"{publisher}.{package}"
        ] if f"{publisher}.{package}" in manager.state['downloads'] else []
    else:
        publisher = target
        matching_packages = [
            pid for pid in manager.state['downloads']
            if pid.split('.', 1)[0].lower().startswith(publisher.lower())
        ]

    if not matching_packages:
        print(f"No packages found matching '{target}'")
        return

    print(f"Found {len(matching_packages)} package(s) matching '{target}':")
    for pkg in matching_packages:
        print(f"  - {pkg}")

    # Ask for confirmation
    confirm = input("Are you sure you want to purge these packages? (yes/no) [no]: ").strip()
    if not confirm:
        confirm = "no"
    if confirm.lower() not in ('yes', 'y'):
        print("Purge cancelled.")
        return

    purged_count = 0
    for package_id in matching_packages:
        package_info = manager.state['downloads'][package_id]
        versions = package_info.get("versions", {})

        for v in list(versions.keys()):
            if version and v != version:
                continue  # skip other versions if specific one requested

            # Remove files from disk
            download_dir = manager.downloads_dir / package_id.split('.', 1)[0] / package_id.split('.', 1)[1] / v
            if download_dir.exists():
                shutil.rmtree(download_dir)

            # Remove from state
            del versions[v]
            purged_count += 1
            print(f"Purged {package_id} {v}")

        # If no versions left, remove package entry entirely
        if not versions:
            del manager.state['downloads'][package_id]

    manager.save_state()
    print(f"Successfully purged {purged_count} version(s)")


@task
def purge_all_packages(c):
    """Purge all downloaded packages.

    Removes downloaded files and state entries for all packages.
    Asks for confirmation before proceeding.

    Example:
        invoke purge-all-packages
    """
    manager = WingetMirrorManager()

    downloaded_packages = manager.state.get('downloads', {})
    if not downloaded_packages:
        print("No downloaded packages found in state.json")
        return

    package_ids = list(downloaded_packages.keys())
    print(f"The following {len(package_ids)} package(s) will be purged:")
    for pkg_id in package_ids:
        print(f"  - {pkg_id}")

    # Ask for confirmation
    confirm = input("Are you sure you want to purge all packages? (yes/no) [no]: ").strip()
    if not confirm:
        confirm = "no"
    if confirm.lower() not in ('yes', 'y'):
        print("Purge cancelled.")
        return

    # Purge all
    purged_count = 0
    for package_id in package_ids:
        pkg = manager.get_package(package_id)
        if pkg.purge():
            purged_count += 1

    print(f"Successfully purged {purged_count} package(s)")

@task
def search(c, target):
    """Search for packages matching publisher or publisher/package.

    Lists all packages from the repository matching the filter,
    along with their download status and versions.
    """
    manager = WingetMirrorManager()
    if not manager.mirror_dir.exists():
        print("Repository not found. Run 'invoke sync-repo' first.")
        return

    downloads = manager.state.get("downloads", {})
    manifests_dir = manager.mirror_dir / "manifests"

    # Parse target
    if "/" in target:
        publisher, package = target.split("/", 1)
        publishers = [publisher]
        package_filter = package
    else:
        publishers = manager.get_matching_publishers(target)
        package_filter = None

    found_packages = []
    for pub in publishers:
        first_letter = pub[0].lower()
        publisher_path = manifests_dir / first_letter / pub
        if not publisher_path.exists():
            continue
        for package_path in publisher_path.iterdir():
            if not package_path.is_dir():
                continue
            if package_filter and package_path.name.lower() != package_filter.lower():
                continue
            package_id = f"{pub}.{package_path.name}"
            found_packages.append(package_id)

    if not found_packages:
        print(f"No packages found matching '{target}'")
        return

    # Collect package data
    package_data = []
    max_pkg_len = len("Package")
    max_status_len = len("Status")

    for package_id in sorted(found_packages):
        pub, pkg = package_id.split(".", 1)
        package_info = downloads.get(package_id, {})
        versions = package_info.get("versions", {})

        if not versions:
            package_data.append((package_id, "Not downloaded", "-", "-"))
            continue

        for v, vdata in versions.items():
            pinned = " (pinned)" if vdata.get("pinned") else ""
            ts = vdata.get("timestamp", "-")
            try:
                dt = datetime.datetime.fromisoformat(ts)
                ts = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

            download_dir = manager.downloads_dir / pub / pkg / v
            if download_dir.exists() and any(download_dir.iterdir()):
                status = "Downloaded"
            else:
                status = "Recorded"

            package_data.append((package_id, status + pinned, v, ts))
            max_pkg_len = max(max_pkg_len, len(package_id))
            max_status_len = max(max_status_len, len(status + pinned))

    # Print table
    print(f"Found {len(found_packages)} package(s) matching '{target}':")
    header = f"{'Package':<{max_pkg_len}}  {'Status':<{max_status_len}}  {'Version':<10}  {'Timestamp':<17}"
    print(header)
    print("-" * len(header))

    for pkg_id, status, ver, ts in package_data:
        print(f"{pkg_id:<{max_pkg_len}}  {status:<{max_status_len}}  {ver:<10}  {ts:<17}")

@task
def patch_repo(c, server_url=None, patch_dir=None):
    """Create patched manifests with corrected InstallerURL paths for downloaded packages.

    Copies manifest files for all downloaded packages to the output directory,
    preserving the same folder structure, and patches InstallerURL to point to
    the local mirror's downloads folder served by the specified server URL.

    This command must be run after downloading packages using 'invoke sync'.

    Args:
        server_url: Base server URL where downloads will be served (e.g., 'https://mirror.example.com')
        patch_dir: Directory to output the patched manifests

    Example:
        invoke patch-repo --server-url="https://mirror.example.com" --patch-dir="./patched-manifests"
    """
    # # Validate server URL
    # if not server_url.startswith(('http://', 'https://')):
    #     print("Error: server_url must start with http:// or https://")
    #     return

    # try:
    #     from urllib.parse import urlparse
    #     parsed = urlparse(server_url)
    #     if not parsed.netloc:
    #         print("Error: server_url must be a valid URL")
    #         return
    # except ImportError:
    #     print("Error: Unable to parse URL")
    #     return

    manager = WingetMirrorManager()

    if not manager.state.get('downloads'):
        print("No downloaded packages found in state.json. Run 'invoke sync' first.")
        return

    manager.patch_repo(server_url=server_url, patch_dir=patch_dir)
    print(f"Patched manifests created in {patch_dir}")

@task
def cleanup(c, dry_run=False):
    """Cleanup old unpinned versions based on config.json thresholds."""
    manager = WingetMirrorManager()
    cfg = manager.config.get("cleanup", {})
    max_versions = cfg.get("max_unpinned_versions", 3)
    max_age_months = cfg.get("max_unpinned_age_months", 6)

    now = datetime.datetime.now()
    cleaned_count = 0

    for package_id, package_info in list(manager.state.get("downloads", {}).items()):
        versions = package_info.get("versions", {})
        if not versions:
            continue

        unpinned = [(v, vdata) for v, vdata in versions.items() if not vdata.get("pinned")]
        if not unpinned:
            continue

        # Sort by timestamp
        unpinned.sort(key=lambda item: parse_version_safe(item[0]))

        # Apply thresholds
        to_delete = []
        if len(unpinned) > max_versions:
            to_delete.extend(unpinned[:-max_versions])

        for v, vdata in unpinned:
            ts = datetime.datetime.fromisoformat(vdata.get("timestamp"))
            age_months = (now.year - ts.year) * 12 + (now.month - ts.month)
            if age_months > max_age_months and (v, vdata) not in to_delete:
                to_delete.append((v, vdata))

        # Delete selected versions
        pkg = manager.get_package(package_id)
        for v, _ in to_delete:
            if dry_run:
                print(f"[DRY RUN] Would clean {package_id} {v}")
            else:
                if pkg.purge(version=v):
                    cleaned_count += 1

    if not dry_run:
        print(f"Cleanup removed {cleaned_count} version(s)")
    else:
        print("Dry run complete — no changes made.")
