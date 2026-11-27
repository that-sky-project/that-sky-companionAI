"""只读 r12 指令和公开路线，不启动服务或读取账号。"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
import xg_paths
from tour_preview import main,parse_command,preview


class TourWords(unittest.TestCase):
    def test_start_and_all(self):
        for text,whole in [('/巡游',False),('小光，巡游吧！',False),('／巡游',False),('全图巡游',True)]:
            self.assertEqual(parse_command(text),('start',{'all':whole,'from':None}))

    def test_from_places_and_slash(self):
        for txt,whole in [('/从晨岛开始巡游',False),('/巡游全部从晨岛开始',True),('小光： /由晨岛跑图全部',True)]:
            self.assertEqual(parse_command(txt),('start',{'from':'晨岛','all':whole}))

    def test_pause_resume_stop(self):
        for text,result in [('/停下',('pause','soft')),('/继续跑',('resume','soft')),('/继续巡游',('resume',None)),
                            ('/停止巡游',('stop',None)),('巡游状态',('status',None)),('跳过',('skip',None))]:
            self.assertEqual(parse_command(text),result)

    def test_unrecognized_stays_unrecognized(self):
        for text in [None,12,b'',{},'','从'+'图'*26+'开始巡游','我在说巡游，不是发指令']:
            self.assertIsNone(parse_command(text))


class TourTable(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests')
        self.addCleanup(self.tmp.cleanup)
        self.control=Path(self.tmp.name)/'中文 空格'
        p=mock.patch.object(xg_paths,'CONTROL',self.control);p.start();self.addCleanup(p.stop)
        p=mock.patch.object(xg_paths,'CONFIG_DIR',ROOT/'app/config');p.start();self.addCleanup(p.stop)
        self.doc=xg_paths.read_tour_routes()

    def test_r12_projection_and_finite_route_counts(self):
        self.assertEqual(self.doc['route_revision'],'T103')
        self.assertEqual(len(self.doc['maps']),49)
        self.assertEqual(sum(len(m['waypoints']) for m in self.doc['maps']),2295)
        required={'scene','map_id','label','region','default','travel_query','waypoints'}
        for m in self.doc['maps']:
            self.assertTrue(required<=m.keys()<=required|{'auto_when_event'})
        self.assertFalse(self.control.exists())

    def test_start_does_not_auto_enable_event(self):
        normal=preview('/巡游',self.doc)
        full=preview('巡游全部',self.doc)
        self.assertEqual((normal['地图数'],normal['活动候选数']),(43,1))
        self.assertEqual((full['地图数'],full['路径点数']),(49,2295))
        self.assertFalse(self.control.exists())

    def test_nondefault_named_map_begins_there(self):
        m=next(m for m in self.doc['maps'] if not m['default'] and not m.get('auto_when_event'))
        r=preview('/从'+m['scene']+'开始巡游',self.doc)
        self.assertEqual(r['起点'],m['label'])
        self.assertEqual(preview('/从不存在的测试地图开始巡游',self.doc),{'错误':'未找到起点'})

    def test_exact_name_beats_fuzzy_earlier_route(self):
        rows=[
            {'scene':'晨岛试验图','default':True,'waypoints':[]},
            {'scene':'晨岛','default':False,'waypoints':[[0,0,0]]},
            {'scene':'云野','default':True,'waypoints':[]},
        ]
        self.assertEqual(preview('/从晨岛开始巡游',{'maps':rows}),
                         {'指令':'开始巡游','地图数':2,'路径点数':1,'起点':'晨岛','活动候选数':0})

    def test_bad_event_flag_in_override_falls_back(self):
        self.control.mkdir()
        row=dict(self.doc['maps'][0],auto_when_event='false')
        (self.control/'tour_routes.json').write_text(json.dumps({'maps':[row],'schema':self.doc['schema']}),
                                                    encoding='utf-8')
        self.assertEqual(xg_paths.read_tour_routes(),self.doc)

    def test_duplicate_local_map_id_falls_back_without_partial_merge(self):
        self.control.mkdir()
        row=dict(self.doc['maps'][0],waypoints=[])
        (self.control/'tour_routes.json').write_text(json.dumps({'maps':[row,row],'schema':self.doc['schema']}),
                                                    encoding='utf-8')
        self.assertEqual(xg_paths.read_tour_routes(),self.doc)

    def test_old_user_table_keeps_precedence(self):
        self.control.mkdir()
        old={'schema':self.doc['schema'],'maps':[{'scene':'旧图','map_id':1,'travel_query':'旧图','waypoints':[]}]}
        (self.control/'tour_routes.json').write_text(json.dumps(old,ensure_ascii=False),encoding='utf-8-sig')
        self.assertEqual(preview('/巡游',xg_paths.read_tour_routes())['起点'],'旧图')

    def test_empty_table_is_a_read_only_noop(self):
        self.assertEqual(preview('/巡游',{'maps':[]}),{'指令':'开始巡游','地图数':0,'路径点数':0,'起点':None,'活动候选数':0})
        self.assertEqual(preview('/继续跑',{'maps':[]}),{'指令':'继续','软指令':True})
        self.assertFalse(self.control.exists())


class LegacyConsole(unittest.TestCase):
    def test_argparse_help_and_error_do_not_crash_on_cp437(self):
        for argv,status,channel in [(['--help'],0,'stdout'),(['--非法'],2,'stderr')]:
            with self.subTest(argv=argv):
                raw={key:io.BytesIO() for key in ('stdout','stderr')}
                streams={key:io.TextIOWrapper(value,encoding='cp437') for key,value in raw.items()}
                try:
                    with mock.patch.multiple(sys,**streams):
                        with self.assertRaises(SystemExit) as error:
                            main(argv)
                    self.assertEqual(error.exception.code,status)
                    streams[channel].flush()
                    self.assertTrue(raw[channel].getvalue())
                finally:
                    for stream in streams.values():
                        stream.close()
