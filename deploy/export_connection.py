"""Explicit operator export. Never overwrites a file or prints device secrets."""
import json
import os
from pathlib import Path
import sys

def connection():
    state=Path(os.environ['POCKET_STATE_DIR'])
    config=json.loads((state/'settings.json').read_text('utf-8'))
    return {'host':os.environ['POCKET_PUBLIC_HOST'],'port':int(os.environ.get('POCKET_PUBLIC_PORT','443')),'tls':True,'path':'/device',
            'token':config['device_token'],'ca':Path(os.environ['POCKET_CA_FILE']).read_text('utf-8')}

def main():
    destination=Path(sys.argv[1]).expanduser().resolve()
    package=connection()
    descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as output:
        json.dump(package,output,ensure_ascii=False,indent=2)
    if hasattr(os,'chown') and os.geteuid()==0 and os.environ.get('SUDO_UID'):
        os.chown(destination,int(os.environ['SUDO_UID']),int(os.environ.get('SUDO_GID','0')))
    print('Private connection configuration written:',destination)

if __name__=='__main__':
    main()
