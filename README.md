# Codex Pocket Cloud

Waveshare ESP32-C6-ePaper-1.54 �� Wi-Fi ����������������ƶ� Codex ���ء�

```text
ESP32������ / ��˷� / īˮ����
    �� Wi-Fi + WSS
��Ѷ���¼��·�������Caddy �� ���� �� Codex app-server
                              ���� ���� Whisper ����ʶ��
```

������������ɺ󣬼���ĵ��Կ��Թػ���Ĭ���� **Codex �������Ѷ�Ʒ�������ִ������**��ģ�������� Codex �������ⲻ���Զ����� OpenAI �йܵ� Codex Cloud ���񡣴˲ֿ�����������ã�ʹ����ͨ���ٷ�������Ȩ�� Codex �˻���

## һ������

�Ƽ�ʹ�� **[Docker ����](DOCKER.md)**��������ֻ�� Docker Engine + Compose v2������ Node/Python/Caddy �������������� 80/443 ��վ��

```bash
git pull
bash deploy/docker.sh up --ip 43.160.215.253 --https-port 8448
bash deploy/docker.sh export ./connection-private.json
```

����Ϊ������ԭ�� systemd ��װ��ʽ��

֧�� Ubuntu 22.04 / 24.04��Debian 12 / 13��x86_64 �� arm64���������� 2 �� 4GB �ڴ桢8GB ���ô��̣�С�ڴ��ѡ `--model base`��Whisper small ģ��Լ 484MB����Ҫ�������ɷ��� npm��GitHub��Hugging Face��OpenAI ��֤��ǩ������

�Ƚ�һ�������� A ��¼ָ����������� IP��������Ѷ�ư�ȫ����������������ǽ���� **TCP 80��443**���������� SSH �˿ڣ��ű������޸� SSH �����ǽ����

```bash
git clone https://github.com/castoryan-looper/codex-pocket-cloud.git codex-pocket-cloud
cd codex-pocket-cloud
sudo bash deploy/install.sh --domain pocket.example.com
```

���԰��豸����˿ڸ�Ϊ 8443�����磺

```bash
sudo bash deploy/install.sh --domain pocket.example.com --https-port 8443
```

��ʱ���� TCP **80 �� 8443**���豸���� `wss://pocket.example.com:8443/device`���������������û������ѡ�˿ڡ���ģʽͨ�� HTTP-01 �Զ�����������֤�飬���� 80 ����ɴ��ҿ��ɴ˷��������443 �����������з����� 80 Ҳ��ռ�ã���Ҫ�������з���������������� DNS ��֤�������豸�˿��޷����֤����֤�˿����⡣����ʱ����������ͬ�� `--https-port`��

Ҳ֧�ֲ�ʹ��������ֱ��ʹ�÷��������� IPv4��

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8443
```

IP ģʽʹ�� Let's Encrypt `shortlived` ����֤�飬Caddy �Զ����ڣ�������� TCP **80 �� 8443**���� 80 ���ɴ˷������������ DNS �������豸ʹ�� `wss://YOUR_PUBLIC_IP:8443/device`����������µ��������á�֤��ǩ���� ESP32 �� IP ֤��������Ҫ��ʵ�ʲ������֤������У��ͨ���������Ѿ���ͨ������ֻ�����豸�ӿںͽ�����飻������������̨��ͨ�� SSH ת�����ʡ�

### ���з���ռ�� 80 �˿�

��ѡ�� TLS-ALPN-01 ֤����֤������ԭ�� Node / Nginx ��վ��

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8448 --acme-challenge tls-alpn-01
```

��ģʽ��Ҫ **443 �� 8448** ���в��Թ������ţ��Լ������ڲ� 8787 ���С�Caddy ����� 443 �� 8448�������� 80��֤����֤������ʹ�� 443���豸����������ʹ�� 8448��443 Ҳ���ṩ��ͬ���Ѽ�Ȩ�豸�ӿڡ��� 443 Ҳ����������ռ�ã���Ҫ��ֹ���з�����Ҫ���������з���������ṩ�Լ���֤�顣����ʱ���� `--acme-challenge tls-alpn-01` �Ͷ˿ڲ�����

### 80 �� 443 ����������վռ��

����ʹ��˽�� CA ģʽ��ֻռ��ָ�����豸�˿ڣ��Լ������ڲ� 8787����

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8448 --tls-mode private-ca
sudo pocketctl export /root/connection-private.json
```

ֻ����� TCP **8448**��Caddy �ڷ���������˽�� CA ���Զ������豸����֤�飻��װ����ֻ���ƹ�����֤�飬CA ˽Կ�����ڷ���˽��Ŀ¼���� connection-private.json ͨ�����ŵ� SSH/SCP ���ز����� ESP32 ���ܱ�������ҳ�棬���а���������֤����豸��Կ���豸���ø�֤����֤�����������ر� TLS У�顣��Ҫɾ�� `/var/lib/codex-pocket-tls`�������֤��ı�������µ��롣����ʱ���� `--tls-mode private-ca` �Ͷ˿ڲ�����

˽��֤�鲻���Զ�����ͨ��������Σ�ʹ�� `sudo pocketctl health` ��֤��������ʽ���ر������֤�顣������ҳ����ʹ�� SSH ת���� 127.0.0.1:8787������Ҫ��ϵͳ���������װ CA��Ҳ�����ƹ������֤�龯�档�������ϵ� Node ��վ����ʹ��ԭ���� 80/443��

�״ΰ�װ����ʾ�ٷ� OpenAI �豸��¼��ַ��һ���Դ��룬�������Լ����������¼�����˺���δ�����豸���¼�������˺Ű�ȫ�������ã�Ҳ�ɰ��ٷ��ĵ��� SSH ת�� `localhost:1455` ����������¼����Ҫ���˺����ơ������ auth.json �ύ�� Git��

�޽�����װ���ڵȴ���¼ʱ�˳��� 2��������У�

```bash
sudo pocketctl login
sudo systemctl start codex-pocket-tls.service
sudo pocketctl health
```

��װ�ű��᣺

- ��װ Node.js 22 LTS���̶�Э����� Codex CLI `0.163.0`��Python �������������塣
- �ӹٷ�������ַ���� Node/Caddy����У�鷢�����ṩ�� SHA-256 / SHA-512��
- ���ر��� Whisper ģ�ͣ�����¼�������ⲿ��������
- ���������ĵ�Ȩ���˻���˽������Ŀ¼�� systemd ������������
- �Զ����������� Let's Encrypt ֤�飬ֻ�Թ������� `/device` �� `/healthz`��
- ����������̨������ `127.0.0.1:8787`���豸��Կ�������ɲ��־ñ��档
- ��鱾���빫������״̬������δ��֤ʱ���˳��� 3 ���棬����ѡ�����������ð�䡰�������á���

��� 80����ѡ HTTPS �˿ڻ� 8787 ������������ռ�ã��״ΰ�װ��ֹͣ��������������վ��TencentOS/CentOS ��֧�ִ� apt ��װ�ű�����ʹ����֧��ϵͳ��������Ǩ�Ʋ������á�

## ���豸����������

���������У�

```bash
sudo pocketctl export /home/ubuntu/connection.json
```

·���ĳ����Լ����û�Ŀ¼���ļ��� 0600 Ȩ�޴�����sudo �û����Ϊ�ļ������ߣ��Ѵ����ļ����ᱻ���ǡ��� SCP ���ص����������ϴ��� Git��

ESP32 ͬʱ��ס BOOT + PWR Լ 3 �룬������Ļ��ʾ�� `CPOCKET-��` �����ȵ㣬��������� `http://192.168.4.1/`������Ļ KEY ��¼������ `connection.json`����д 2.4GHz �ȵ㲢���档�豸��ʹ�� `wss://�������/device`������ USB �����Žӡ�

����豸�����Ӿ�������Ҳ������������� IP ��������ҳ��KEY �����豸������Ļ��ʾ���������ز���������������롣

## ����������

```bash
sudo pocketctl status
sudo pocketctl logs
sudo pocketctl restart
sudo pocketctl health
```

��������̨ͨ�� SSH ת�����ʣ�

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@��ķ�����
```

���������� `sudo pocketctl console`��������ı�������̨���Ӹ��Ƶ�����������������Ӻ�˽�˷������ƣ���Ҫ������

������

```bash
git pull
sudo bash deploy/install.sh --domain pocket.example.com
```

���������� `/var/lib/codex-pocket`����Դ�뷢��Ŀ¼���롣���ܲ�������¼���豸��Կ���Ự��ģ�ͺ͹���Ŀ¼���ɷ��������� `/opt/codex-pocket/releases` �����˹����ˣ����� Python �����󣬻��˴���ʱҲ�������������ԡ�

## �豸����

- PWR �̰�ѡ�˵���BOOT �̰�ִ�С�
- BOOT ��ס 1 �뿪ʼ¼�����ɿ�����ֹͣ��� 20 �롣¼��״̬���������豸���ػ��ƣ����紦����������������ѯ��
- ����תд����ʾ�ݸ壬ȷ�Ϻ�ŷ��͸� Codex��
- ¼���а� PWR ȡ����˫����ס 3 ���������������ʱ PWR ����ֹͣ��������ģʽ�¹ر������ȵ㡣
- ��ҳ������ Wi-Fi���ƶ˵�ַ/��Կ/֤�顢¼��������ά��ȫˢ�����
- Wi-Fi / USB ͼ�����豸���ظ��¡�USB ��������¼��������־��������ҳ���ã�������������ֻͨ�� Wi-Fi��
- ����ʣ�������� Codex ��ʵ���ڰٷֱȣ������鹹��ʣ�� token ����
- ������ ADC ��ѹ���㣻���ʱ����ƫ�ߡ�

## ִ��Ȩ����߽�

Ĭ�Ϲ���Ŀ¼�Ƿ������� `/var/lib/codex-pocket/workspace`��ʹ�� workspace-write �� on-request �����������ܷ�����ҵ��Եı�����Ŀ������������ͬ���������Ŀ����׼������Ҫͨ��˽�й�������̨���豸֧�־ܾ���ֹͣ���˰汾���ṩ���⹫���ļ������������׼��

����Ŀ��δʵ�� OpenAI �й� `codex cloud exec --env ...` �����������������Ҫ��ģʽ����Ҫ�������� Codex Cloud ��Ŀ���������ܰѵ�ǰ�������ϵ� app-server ����Ϊ�ٷ��йܻ�����

## �̼�����

`firmware/` �����Ѿ���ʵ�����֤����Դ���볧�����������ֿⲻ���������豸���ݻ�Ԥ��ƾ�ݡ�Arduino ESP32 3.3.0����װ ArduinoJson��WebSockets��OneButton ���� firmware/libraries��

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.0 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install ArduinoJson WebSockets
arduino-cli compile --fqbn 'esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB' --libraries firmware/libraries --output-dir build firmware/Pocket
```

Windows Ҳ��ʹ�� build.ps1��ֻ����Ӧ�õ� 0x10000 �ɱ��� NVS��ˢ�����ϲ��̼�������豸���á�

## ��������

```bash
python -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
POCKET_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc .venv/bin/python -m unittest discover -s bridge -p 'test_*.py' -v
bash -n deploy/install.sh
bash -n deploy/pocketctl
```

��װ�ű���Ҫ��ʵ Linux/systemd���������˻���Ȩ�����ؾ�̬�����������Բ��������������ϵĹ���������֤����֤��Χ�� TESTING.md��

## �ֿ�ַ�

���յ����� Git bundle��`git clone codex-pocket-cloud.bundle codex-pocket-cloud`������������ Git �ύ��¼�������������Լ���Զ�ֿ̲Ⲣ���ͣ����ڷ��������� git clone / git pull�������زֿ���������Ѵ��� GitHub Զ�ֿ̲⡣

## �ٷ��ο������������

- [Codex headless authentication](https://learn.chatgpt.com/docs/auth)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

����������Դ��ĸ�������֤���� THIRD_PARTY.md��Waveshare ����û�ж�������֤�������ַ�ǰӦ��ʵ�����Ȩ�����˲���Ӧ�����κ�����ƾ�ݡ�

## ���ػ����� PyPI �����ϣ����

Python ����Ĭ����ʽʹ�ùٷ� `https://pypi.org/simple`�����԰�װ���̺���ϵͳԤ���õľ��񣬲��޸�ϵͳ pip ���û�ر� TLS/��ϣУ�顣����������ֹ�ϣ��һ��ʱ�����²ֿⲢ���ܼ��ɸ��ùٷ�Դ����������Ч��ȡ���ڷ��������硣���ܻḴ���Ѱ�װ�� Node.js 22����ͬ�汾 Codex CLI�������ȶ��� Caddy�����ⷴ�����ء�����ʽ���� CADDY_VERSION ѡ���°汾��

���ٷ�Դ����������ָ������ȷ�ϵ� HTTPS PyPI Դ��`sudo env POCKET_PIP_INDEX_URL=https://YOUR_TRUSTED_INDEX/simple bash deploy/install.sh ...`���ñ����������˺�ƾ��ʱ���ʺ�ֱ��д�������Ҫ�ùرչ�ϣ��֤��У��ķ�������ʧ�ܡ�

����ģ����δ����ʱ��`--model base` �ɼ���ģ�����������ڴ�ռ�ã���ͨ���ή������ʶ��׼ȷ�ȣ�Ĭ����Ϊ small��

�̼� 0.3.0 �ṩ���ذ�ס��ʾ��¼����ʱ���ϴ�/ʶ����쳣ҳ�棬������ȷ��ʾ�ƶ�����/���ߡ�īˮ����������ˢ��ʱ�䣬���ܱ�֤�� LCD һ��˲ʱ�仯��¼�����Ʋ��ȴ���Ļ���ƶ����ˢ�¡���������ͬ�����£�ʹ�ýṹ������״̬��ʱ�л�ʶ�����������ر������ݻ��ˡ�

�̼� 0.3.3 ��¼���ݴ����豸 FAT �������ɿ� BOOT ��ֿ�ͨ�� Wi-Fi �ϴ�����ʾ���ȣ��������緢��ͣ�ټ���ʵʱ¼�����С���ס�� 1 �뿪ʼ���ɿ�ֹͣ��PWR ����ȡ��������ֻ������ǰһ�Σ�� 20 �룩���ϴ�������ȡ����ʧ�ܻ��豸����ʱɾ�������ָ�δ�ύ¼����ֻ�Զ���ʼ����ȫ������ FAT ����������ʽ�������ļ�ϵͳ�������޸��ƶ���ƵЭ�顣


## USB ������ʵʱ�豸״̬���̼� 0.4.0��

Windows ���� USB �����ߺ�˫���ֿ��е� `USB����.cmd`�����Զ��򿪱���������ҳ��Ĭ�ϵ�ַΪ `http://127.0.0.1:8788/`����һ�λ��ڱ���Ŀ�Ķ���������װ pyserial�����޸�ȫ�� Python�����ֹ������У����ɰβ��豸���Զ�����������ѵ����е��豸�ȵ㣬Ҳ����Ҫ�ƶ����������������رչ��ߺ��´�����˫���������ظ������������ҳ�档

ҳ��ÿ 2 ���ȡ�豸����ʵ״̬����ǰ���ѱ��� Wi-Fi���豸 IP���ź�ǿ�ȡ�������֤���ƶ����ӡ�USB���̼�/оƬ/Flash/�ڴ桢�������㡢¼�����漰�ϴ����ȡ�����������֤����ʾ�豸ʵ���������ƶ˼���ͨ���������� Wi-Fi ʱ��ʾ������֤�������� Wi-Fi ͼ�굱��������ͨ���޸����ú�㡰���沢�����豸����ͬһ Wi-Fi ���뼰�����豸��Կ���ջᱣ������ֱ�ӵ����ƶ����� JSON��

USB ͨ����֧�ֶ�ȡ״̬���豸���á��������ü��ر������ȵ㣬���ṩ����ָ�������ת������ҳֻ�������Իػ���ַ��У�鱾������ƾ�ݡ�Host �� Origin��ƾ���ɱ���ҳ���Զ���ʼ�����豸����/��Կ����ͨ����ѯ�ӿڷ��أ�Ҳ��д�������־��Wi-Fi ����ҳ���Կ�ͨ��˫���������룬��Ҫ����Ļ�ϵ� KEY��

���ԣ�`python -m unittest discover -s tools -p test_usb_config.py`��Linux/macOS ���ڰ�װ pyserial ������ `python tools/usb_config.py --open`��Windows ˫�����Ϊ��Ҫ��֤·����

�̼� 0.5.2 ֧�����豸�� USB ������ҳɨ�貢ѡ�񸽽��� 2.4 GHz Wi-Fi�����������Կ��ֶ����롣ɨ���ڼ��豸����ݶϿ� Wi-Fi ������ɺ�ָ����豸��������ʾ��ǰ���ӵ� Wi-Fi ���ƣ���ҳ����ʱΪ�������ĵ�����������Ļ���Ρ�USB ���ó���Ϣ�ֶη��ͣ������ڼ���ʾ������ʾ��

���������º��豸���������Э�� 8 kHz G.711 ��-law �������룬��Ƶ����ԼΪԭʼ 16 kHz��16 λ PCM ���ķ�֮һ������һ�������������룬ʵ��ʶ��Ч����Ҫ�ֳ���֤���ɷ�����δ����������ʱ��ʹ��ԭ��ʽ���ϴ������жϺ�ᱣ���豸���棬����Զ����� 3 �Σ�����ʾ���Դ�����ȡ������ɡ�����ʧ�ܻ�����ʱ������档��Ƶ�ϴ�����ֱ��ִ������ʶ��ݸ�����ȷ�ϡ�
