"""Run the actual portal transition functions against fake USB/Wi-Fi hardware.

Requires a host C++ compiler (run from a Visual Studio developer shell on Windows).
"""
import os
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ConfigPriorityTests(unittest.TestCase):
    def test_connection_transitions(self):
        compiler = shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
        if not compiler:
            self.skipTest('host C++ compiler required')
        header = (Path(__file__).resolve().parents[1] / 'firmware/Pocket/config_portal.h').read_text(encoding='utf-8')
        start = header[header.index('static void startConfigPortal(bool automatic=false) {'):header.index('static void initConfigPortal(')]
        loop = header[header.index('static void loopConfigPortal()'):]
        harness = r'''
#include <cassert>
#include <cstdint>
#include <string>
bool usb=false, portalActive=false, portalAutoOpened=false, configModeActive=false, imageDirty=false;
uint32_t now=31000, portalRestartAt=0, portalCloseAt=0, portalBootAt=0;
int offlineStage=0;
std::string portalSsid="test", portalKey="test-key";
enum {WIFI_AP_STA, WL_CONNECTED};
struct {int state=-1, clients=0; bool ap=false, fail=false;
 void mode(int){} bool softAP(const char*,const char*){return ap=!fail;}
 int status(){return state;} int softAPIP(){return 0;}
 void softAPdisconnect(bool){ap=false;} int softAPgetStationNum(){return clients;}
} WiFi;
struct {bool active=false; void start(int,const char*,int){active=true;}
 void stop(){active=false;} void processNextRequest(){} } configDns;
struct {void handleClient(){} } configWeb;
struct {void restart(){} } ESP;
struct HWCDC {static bool isPlugged(){return usb;}};
uint32_t millis(){return now;}
'''
        harness += start + loop + r'''
int main(){
 // Host data connection suppresses initial and repeated offline fallback.
 usb=true; startConfigPortal(true); loopConfigPortal();
 assert(!portalActive && !WiFi.ap && !configDns.active && configModeActive);
 // Charger/charge-only cable has no USB bus: offline AP is available.
 usb=false; loopConfigPortal();
 assert(portalActive && portalAutoOpened && WiFi.ap && configDns.active);
 // Plugging into a host closes even an AP with connected clients.
 WiFi.clients=1; usb=true; loopConfigPortal();
 assert(!portalActive && !WiFi.ap && !configDns.active && configModeActive);
 // USB has a visible configuration mode even without an AP.
 WiFi.state=WL_CONNECTED; startConfigPortal(); loopConfigPortal();
 assert(!portalActive && configModeActive);
 usb=false; loopConfigPortal();
 assert(portalActive && !portalAutoOpened && configModeActive);
 // Manual AP preference survives a USB insertion/removal cycle.
 usb=true; loopConfigPortal(); assert(!portalActive && configModeActive);
 usb=false; loopConfigPortal(); assert(portalActive && !portalAutoOpened);
 // Explicit close exits configuration mode.
 portalCloseAt=now; loopConfigPortal();
 assert(!portalActive && !configModeActive);
 loopConfigPortal(); assert(!portalActive);
 // Automatic recovery AP still closes after Wi-Fi reconnects.
 WiFi.clients=0; WiFi.state=-1; loopConfigPortal();
 assert(portalActive && portalAutoOpened);
 WiFi.state=WL_CONNECTED; loopConfigPortal(); assert(portalCloseAt);
 now+=501; loopConfigPortal(); assert(!portalActive);
 // Failed AP startup can retry while configuration mode stays active.
 WiFi.fail=true; startConfigPortal(); assert(!portalActive && configModeActive);
 WiFi.fail=false; loopConfigPortal(); assert(portalActive && !portalAutoOpened);
}
'''
        build_dir = os.environ.get('POCKET_TEST_BUILD_DIR')
        with (nullcontext(build_dir) if build_dir else tempfile.TemporaryDirectory()) as directory:
            source = Path(directory) / 'priority.cpp'
            source.write_text(harness, encoding='utf-8')
            exe = Path(directory) / ('priority.exe' if os.name == 'nt' else 'priority')
            if Path(compiler).name.lower() == 'cl.exe':
                command = [compiler, '/nologo', '/EHsc', str(source), '/Fe:' + str(exe)]
            else:
                command = [compiler, '-std=c++11', str(source), '-o', str(exe)]
            subprocess.run(command, cwd=directory, check=True, capture_output=True)
            subprocess.run([str(exe)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
