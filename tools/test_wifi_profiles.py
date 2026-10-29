"""Compile the actual Wi-Fi policy and reconnect controller with fake hardware."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


class WifiProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
        if not compiler:
            raise unittest.SkipTest('host C++ compiler required')
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.exe = cls.directory / ('wifi-tests.exe' if os.name == 'nt' else 'wifi-tests')
        root = Path(__file__).resolve().parents[1] / 'firmware/Pocket'
        source = cls.directory / 'wifi-tests.cpp'
        source.write_text(HARNESS, encoding='utf-8')
        if Path(compiler).name.lower() == 'cl.exe':
            command = [compiler, '/nologo', '/EHsc', '/utf-8', '/std:c++14', '/I'+str(root), str(source), '/Fe:'+str(cls.exe)]
        else:
            command = [compiler, '-std=c++11', '-I', str(root), str(source), '-o', str(cls.exe)]
        result = subprocess.run(command, cwd=cls.directory, capture_output=True)
        if result.returncode:
            raise AssertionError(result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def check(self, case):
        result = subprocess.run([str(self.exe), case], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_capacity_lru_and_current_network_protection(self): self.check('capacity')
    def test_password_retention_update_and_explicit_open_network(self): self.check('password')
    def test_preferred_strength_and_cooldown_selection(self): self.check('selection')
    def test_open_and_secured_networks_are_not_confused(self): self.check('security')
    def test_forgetting_preferred_and_preserving_other_passwords(self): self.check('forget')
    def test_byte_limits_and_invalid_network_names(self): self.check('validation')
    def test_recency_counter_and_deadline_wraparound(self): self.check('wrap')
    def test_real_controller_fails_over_without_roaming_working_connection(self): self.check('failover')
    def test_manual_scan_resumes_actual_network_not_preferred(self): self.check('manual')
    def test_audio_and_ui_scans_pause_background_search(self): self.check('busy')
    def test_hidden_network_fallback_and_no_security_downgrade(self): self.check('hidden')
    def test_forget_storage_failure_is_non_destructive(self): self.check('storage')
    def test_background_scan_timeout_recovers(self): self.check('timeout')
    def test_offline_upload_and_recognition_can_recover_network(self): self.check('offline_upload')


HARNESS = r'''
#include <cassert>
#include <string>
#include <vector>
#include <map>
#include <cstring>
#include "wifi_profile_policy.h"
using String=std::string;
static uint32_t now=100;
uint32_t millis(){return now;}
enum {WL_CONNECTED=3,WL_DISCONNECTED=6,WIFI_SCAN_RUNNING=-1,WIFI_SCAN_FAILED=-2,WIFI_AUTH_OPEN=0,WIFI_AUTH_WPA_PSK=1};
struct AP {String ssid;int rssi;bool secure;};
struct MockWifi {
 int state=WL_DISCONNECTED,scanState=0,begins=0,scans=0,minSecurity=-1;bool autoReconnect=false;
 String actual,target,password;std::vector<AP> aps;
 int status(){return state;}String SSID(){return actual;}
 String SSID(int i){return aps[i].ssid;}int RSSI(int i){return aps[i].rssi;}
 int encryptionType(int i){return aps[i].secure?WIFI_AUTH_WPA_PSK:WIFI_AUTH_OPEN;}
 void setAutoReconnect(bool value){autoReconnect=value;}
 void setMinSecurity(int value){minSecurity=value;}
 void begin(const char *s,const char *p){begins++;target=s;password=p;state=WL_DISCONNECTED;}
 void scanDelete(){}int scanNetworks(bool,bool,bool,int){scans++;return scanState;}
 int scanComplete(){return scanState;}
} WiFi;
int esp_wifi_disconnect(){WiFi.state=WL_DISCONNECTED;return 0;}
int esp_wifi_scan_stop(){WiFi.scanState=WIFI_SCAN_FAILED;return 0;}
struct JsonDocument {
 std::map<std::string,std::string> fields;
 std::string &operator[](const char *key){return fields[key];}
};
struct Preferences {
 bool begin(const char*,bool){return true;}
 void putString(const char*,const String&){}void remove(const char*){}void end(){}
};
static KnownWifiSet knownWifi={};static bool knownWifiPersisted=false,saveWorks=true;
static bool wifiConfigured=false,ssidBitmapValid=true,recording=false,uploadPending=false,captureRequested=false,syntheticRequested=false,remoteVoiceBusy=false;
static int wifiScanPhase=0,lastWifiDisconnectReason=0;
static String wifiSsid,wifiPass;
bool persistKnownWifi(const KnownWifiSet&){return saveWorks;}
void syncPreferredWifi(){int i=knownWifiFind(knownWifi,knownWifi.preferred);wifiConfigured=knownWifi.count>0;wifiSsid=i<0?"":knownWifi.profiles[i].ssid;wifiPass=i<0?"":knownWifi.profiles[i].password;}
#include "wifi_reconnect.h"
static void add(const char *name,const char *password="fixture-password"){assert(knownWifiUpsert(knownWifi,name,password,true));}
int main(int argc,char **argv){
 assert(argc==2);String test=argv[1];
 if(test=="capacity"){
  for(int i=0;i<8;i++){String s="net"+std::to_string(i);add(s.c_str());}
  assert(knownWifi.count==8);assert(knownWifiUpsert(knownWifi,"ninth","new-password",true,"net0"));
  assert(knownWifi.count==8&&knownWifiFind(knownWifi,"net0")>=0&&knownWifiFind(knownWifi,"net1")==-1);
  assert(String(knownWifi.preferred)=="ninth");
 }else if(test=="password"){
  add("home","home-password");add("work","work-password");
  assert(knownWifiUpsert(knownWifi,"home","",false));
  assert(knownWifi.count==2&&String(knownWifi.profiles[knownWifiFind(knownWifi,"home")].password)=="home-password");
  assert(knownWifiUpsert(knownWifi,"home","new-password",true));
  assert(String(knownWifi.profiles[knownWifiFind(knownWifi,"home")].password)=="new-password");
  assert(knownWifiUpsert(knownWifi,"home","",true));assert(!knownWifi.profiles[knownWifiFind(knownWifi,"home")].password[0]);
 }else if(test=="selection"){
  add("strong");add("preferred");KnownWifiVisible aps[]={{"strong",-30,true},{"preferred",-75,true}};
  assert(knownWifiChoose(knownWifi,aps,2,wifiBlockedUntil,now)==1);
  wifiBlockedUntil[1]=now+1000;assert(knownWifiChoose(knownWifi,aps,2,wifiBlockedUntil,now)==0);
  assert(knownWifiChoose(knownWifi,aps,2,wifiBlockedUntil,now+1001)==1);
 }else if(test=="security"){
  add("secure");add("open","");KnownWifiVisible aps[]={{"secure",-20,false},{"open",-25,true}};
  assert(knownWifiChoose(knownWifi,aps,2,wifiBlockedUntil,now)==-1);
  aps[1].secure=false;assert(knownWifiChoose(knownWifi,aps,2,wifiBlockedUntil,now)==1);
 }else if(test=="forget"){
  add("a","pass-a");add("b","pass-b");assert(!knownWifiForget(knownWifi,"missing"));
  assert(knownWifiForget(knownWifi,"b"));assert(String(knownWifi.preferred)=="a");
  assert(String(knownWifi.profiles[0].password)=="pass-a");assert(knownWifiForget(knownWifi,"a"));
  assert(knownWifi.count==0&&!knownWifi.preferred[0]);
 }else if(test=="validation"){
  assert(!knownWifiValid("",""));assert(!knownWifiValid("bad\nname",""));
  assert(!knownWifiValid(String(33,'x').c_str(),""));assert(!knownWifiValid("a",String(64,'x').c_str()));
  assert(knownWifiValid(String(32,'x').c_str(),String(63,'x').c_str()));
 }else if(test=="wrap"){
  add("a");add("b");knownWifi.clock=UINT32_MAX;knownWifiTouch(knownWifi,"a");
  assert(knownWifi.profiles[0].used>knownWifi.profiles[1].used);
  wifiBlockedUntil[0]=20;assert(knownWifiBlocked(wifiBlockedUntil,0,UINT32_MAX-10));
  assert(!knownWifiBlocked(wifiBlockedUntil,0,21));
  assert(wifiDeadlineReached(UINT32_MAX-10,0));
 }else if(test=="failover"){
  add("backup");add("preferred");wifiStartKnown(1);assert(WiFi.target=="preferred");
  now+=2000;lastWifiDisconnectReason=202;loopKnownWifi();assert(wifiBlockedUntil[1]>now);
  now+=600;loopKnownWifi();assert(wifiAutoPhase==1);
  WiFi.aps={{"backup",-45,true},{"preferred",-20,true}};WiFi.scanState=2;
  now+=500;loopKnownWifi();assert(wifiAutoPhase==2);loopKnownWifi();assert(WiFi.target=="backup");
  WiFi.state=WL_CONNECTED;WiFi.actual="backup";loopKnownWifi();int count=WiFi.begins;
  now+=600000;loopKnownWifi();assert(WiFi.begins==count&&wifiAutoPhase==0);
  assert(!WiFi.autoReconnect);
 }else if(test=="manual"){
  add("active");add("preferred");WiFi.state=WL_CONNECTED;WiFi.actual="active";
  wifiPauseForManualScan();esp_wifi_disconnect();loopKnownWifi();assert(WiFi.scans==0);
  wifiResumeAfterManualScan();assert(WiFi.target=="active"&&!wifiManualScan);
 }else if(test=="busy"){
  add("known");recording=true;loopKnownWifi();assert(wifiAutoPhase==0);
  recording=false;captureRequested=true;loopKnownWifi();assert(wifiAutoPhase==0);
  captureRequested=false;wifiScanPhase=1;loopKnownWifi();assert(wifiAutoPhase==0);
 }else if(test=="hidden"){
  add("hidden");add("secured");WiFi.aps={{"secured",-20,false}};WiFi.scanState=1;wifiAutoPhase=2;
  loopKnownWifi();assert(WiFi.target=="hidden"&&WiFi.minSecurity==WIFI_AUTH_WPA_PSK);
 }else if(test=="storage"){
  add("keep");WiFi.state=WL_CONNECTED;WiFi.actual="keep";saveWorks=false;JsonDocument out;
  assert(forgetKnownWifi("keep",out)==500&&knownWifi.count==1&&WiFi.status()==WL_CONNECTED);
  saveWorks=true;assert(forgetKnownWifi("keep",out)==200&&knownWifi.count==0&&WiFi.status()!=WL_CONNECTED);
  assert(out.fields.count("password")==0);
 }else if(test=="timeout"){
  add("known");wifiAutoPhase=2;wifiAutoScanAt=100;now=16100;WiFi.scanState=WIFI_SCAN_RUNNING;
  loopKnownWifi();assert(wifiAutoPhase==0&&wifiNextScanAt>now);
 }else if(test=="offline_upload"){
  add("known");uploadPending=true;remoteVoiceBusy=true;loopKnownWifi();assert(wifiAutoPhase==1);
  wifiAutoPhase=0;WiFi.state=WL_CONNECTED;WiFi.actual="known";int scans=WiFi.scans;
  loopKnownWifi();assert(WiFi.scans==scans&&wifiAutoPhase==0);
 }else return 2;
}
'''


if __name__ == '__main__':
    unittest.main()
