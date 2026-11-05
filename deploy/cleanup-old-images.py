"""Remove old Pocket deployment image tags after pausing automatic deployment."""
import argparse
import json
import subprocess

REPOSITORIES = {'codex-pocket-autodeploy-gateway', 'codex-pocket-autodeploy-ingress'}
KEEP_COMMITS = ('83c77268a991', '9e34336e6a9e', 'feeb33bec9b7')


def docker(*arguments):
    return subprocess.check_output(['docker', *arguments], text=True)


def cleanup_plan(images, used_images):
    protected = {value.removeprefix('sha256:') for value in used_images}
    for image in images:
        if image['Repository'] in REPOSITORIES and image['Tag'].startswith(
                tuple(commit + '-' for commit in KEEP_COMMITS)):
            protected.add(image['ID'].removeprefix('sha256:'))
    return sorted({image['Repository'] + ':' + image['Tag'] for image in images
                   if image['Repository'] in REPOSITORIES
                   and image['Tag'] != '<none>'
                   and image['ID'].removeprefix('sha256:') not in protected})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Delete the listed old image tags')
    args = parser.parse_args()
    if args.apply:
        for unit in ('codex-pocket-autodeploy.timer', 'codex-pocket-autodeploy.service'):
            state = subprocess.check_output(
                ['systemctl', 'show', unit, '--property=ActiveState', '--value'], text=True).strip()
            if state not in ('inactive', 'failed'):
                raise RuntimeError('Stop automatic deployment before cleanup: ' + unit)
    images = [json.loads(line) for line in docker(
        'image', 'ls', '--no-trunc', '--format', '{{json .}}').splitlines()]
    containers = docker('ps', '-aq').split()
    used = json.loads(docker('inspect', *containers)) if containers else []
    plan = cleanup_plan(images, {container['Image'] for container in used})
    print('Old Pocket image tags selected:', len(plan), flush=True)
    for reference in plan:
        if not args.apply:
            print(reference)
            continue
        result = subprocess.run(['docker', 'image', 'rm', reference],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            print('Retained:', reference, result.stdout.strip(), flush=True)
    subprocess.run(['df', '-h', '/var/lib/docker'], check=True)


if __name__ == '__main__':
    main()
