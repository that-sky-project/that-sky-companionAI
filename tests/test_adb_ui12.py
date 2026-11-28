"""Android 15 UI hints and release pins; source-only, no device/config access."""
from pathlib import Path
from html.parser import HTMLParser
import ast
import json
import unittest

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
VERSION='1.0.5-desktop-ui.10.2-terrainfix.20'

class Tags(HTMLParser):
    def __init__(self,text):
        super().__init__(convert_charrefs=True);self.by_id={};self.feed(text)
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if values.get('id'):self.by_id[values['id']]=(tag,values)

class AdbUiVersionTests(unittest.TestCase):
    def test_wizard_android15_path_is_explicit_example(self):
        text=(APP/'web/wizard.html').read_text(encoding='utf-8')
        tags=Tags(text);tag,attrs=tags.by_id['wz-adb']
        self.assertEqual(tag,'input')
        self.assertIn('nx_device\\15.0\\shell\\adb.exe',attrs['placeholder'])
        self.assertTrue(attrs['placeholder'].startswith('例如 '))
        self.assertIn('本机实际安装位置',text);self.assertIn('留空自动查找',text)
        self.assertEqual(tags.by_id['wz-mumu-check'][0],'button')

    def test_mumu_requirement_no_longer_forces_12(self):
        text=(APP/'web/wizard.html').read_text(encoding='utf-8')
        self.assertIn('MuMu（Android 15，兼容旧版）',text)
        self.assertNotIn('需要：MuMu 12',text)
        self.assertIn('安装与当前客户端支持版本一致的光遇',text)
        self.assertNotIn('MuMu 里装好光遇 0.16.7 / 421589',text)

    def test_desktop_resources_keep_manual_override_and_android15_hint(self):
        text=(APP/'web/desktop.js').read_text(encoding='utf-8')
        self.assertIn('支持 MuMu Android 15，并兼容旧版',text)
        self.assertIn("'resource-adb'",text)
        self.assertIn("'/api/wizard/mumu',{adb:byId('resource-adb').value.trim()}",text)

    def test_ui_does_not_require_a_fixed_adb_serial_or_port(self):
        text=(APP/'web/wizard.html').read_text(encoding='utf-8')
        section=text.split("data-panel='mumu'",1)[1].split('</section>',1)[0]
        self.assertIn('实例与端口从设备列表读取',section)
        for value in ('127.0.0.1:16384','127.0.0.1:7555','emulator-5554'):
            self.assertNotIn(value,section)
        js=(APP/'web/wizard.js').read_text(encoding='utf-8')
        self.assertIn('r.serial ? "MuMu 实例：" + r.serial',js)

    def test_worker_failure_hint_no_longer_forces_12(self):
        tree=ast.parse((APP/'capture/worker.py').read_text(encoding='utf-8'))
        messages=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.startswith('没找到 MuMu：')]
        self.assertEqual(len(messages),1)
        self.assertIn('支持 Android 15，兼容旧版',messages[0])
        self.assertIn('本机实际 adb.exe 路径',messages[0])
        self.assertNotIn('确认装了 MuMu 12',messages[0])

    def test_current_runtime_and_visible_client_release_agree(self):
        cfg=json.loads((APP/'config/experimental-hop-trial.runtime.json').read_text(encoding='utf-8'))
        html=(APP/'web/desktop.html').read_text(encoding='utf-8')
        self.assertEqual(cfg['clientRelease'],VERSION)
        self.assertIn('>'+VERSION+'</p>',html)
        self.assertIn('小光 · 桌面 · '+VERSION+'</title>',html)
        self.assertEqual(cfg['hopV20']['codecSHA256'],'0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed')

if __name__=='__main__':unittest.main()
