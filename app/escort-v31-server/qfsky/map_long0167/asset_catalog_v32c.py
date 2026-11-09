"""Exact32 pinned asset catalog trial; not current/native-loaded captures.

No file reads, source account discovery, sockets, deployment or global map
mutation. Frozen v32b primitives validate the genuine saved same-account source.
"""
from __future__ import annotations
from dataclasses import dataclass,field
from datetime import datetime,timezone
import hashlib,hmac,json,math,os,re,struct
from . import asset_bootstrap_v32 as _B
from . import catalog_account_pins as _P
from qfsky.join0167_profile import LoadedProfile,account_binding,NATIVE_SHA256
from .scene_only_draft import SceneBaseline,check_scene_record,_SEAL as _SCENE_SEAL

ENV_ENABLE='QFSKY_ASSET_CATALOG_TRIAL'
ENV_MANIFEST_SHA='QFSKY_ASSET_CATALOG_MANIFEST_SHA256'
MANIFEST_SCHEMA='sky-asset-catalog-trial-v32c'
KIND='asset-bootstrap-experimental'
SOURCE_SHA=_B.SOURCE_SHA
SOURCE_METADATA_SHA=_B.SOURCE_METADATA_SHA
_SEAL=object()

# v33 关卡起点来源：与 v32c LevelLink 来源并列的第二条准入路径。
KIND_LEVEL_LINK='apk_placement_boundary_signed_support_portal_exclusion_engineering_v32c'
KIND_LEVEL_START='apk_level_start_spawn_engineering_v33'
# v35：T1/APK 初始静态碰撞在目标高度没有朝上可站面时，允许用运行时 skyterrain
# beamo static-world 证明上层地板；只对显式带 runtime_support_geometry 的条目生效。
KIND_LEVEL_START_RUNTIME='apk_level_start_spawn_runtime_beamo_v35'
RUNTIME_BEAMO_MANIFEST_SHA='fb9e1b3ebda39ff5ca9684248206a1cf2dac8e737d4d64380caef6a74cf63369'
RUNTIME_BEAMO_POSITIONS_SHA='0ce6dd545838e0ce00f776e973e40b7bdb773e9ae8a44bd2421d4e1c7722aa2b'
RUNTIME_BEAMO_INDICES_SHA='42d48562b959733ce32ece68042cc8355281e46e7ff7074c9c268bdfee762230'
RUNTIME_BEAMO_SOURCE='beamo-world-all-v1/SunsetEnd2/static-world'
# v34 人工标注来源：哥哥在地形工作台里点选的出生点，原始标注记录整条嵌入并 pin。
KIND_USER_ANNOTATION='user_annotated_spawn_studio_v34'
LEVEL_START_ENTRY_TYPES=('AvatarSetPos','Checkpoint','SpawnAvatars')
LEVEL_START_PORTAL_MODEL='engineering_level_start_capped_LevelLink_AABB_vs_capsule_body_AABB'
LEVEL_START_PORTAL_CAP=4.0
LEVEL_START_BOUND_SOURCES=('static_world','render_geometry','object_anchors')
LEVEL_START_FOOTPRINT=frozenset({(0.0,0.0),(0.22,0.0),(-0.22,0.0),(0.0,0.22),(0.0,-0.22)})

def _sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical_json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def _fail(code):raise ValueError('asset_catalog_'+code)

_EXPECTED_ENTRIES_JSON = '{"1058995818":{"actor_sha256":"1399e81c709fe18d98d7fa015100873a040d393036551767f4729aadfbfa151b","cache_sha256":"b5ad2461852db09c79e91782aeeaa7acca598b99a407ba9ab9ddbbf9b484f387","label":"Dusk_PearlSea","level_id":1058995818,"netversion_u16":[40550,49307,23536,0,0,25536],"placement_sha256":"402cd2daabb2e03d5ce50a9eb0b0ee5809580118a14a461130658ff42185bcc3","resource_revision":"apk-v75506-421589","scene":"Dusk_PearlSea","scene_provenance_sha256":"539535b79f394e2c131ed806241437a7e530f13ce9a5bdd880b28da2d97e0297","scene_sha256":"594fab6b774ba2f93b558fe5658d8c82aa99978f73fa8492b0a881ba06f7e956"},"1104763408":{"actor_sha256":"cfdc1218528200fc21080085d8cb187f525cf7ab5a93ebb9a333bb550139ee9f","cache_sha256":"ec93d32274efd90a1911418190a4b76670cf7013c7c24efab49a382510b7d218","label":"MainStreet_Cafe_Wonderland","level_id":1104763408,"netversion_u16":[4688,49307,23536,0,0,25536],"placement_sha256":"2e382785b780d8914d4eeda275af3368043dc483573457dbdb7d8c15bb9530e3","resource_revision":"apk-v75506-421589","scene":"MainStreet_Cafe_Wonderland","scene_provenance_sha256":"e98221777e622826c9bb6433d22fe4a25d07ad187a1606a5a3386c88c46f391a","scene_sha256":"d5f01b3241cf1a25c6921e2ffb4bd50db53fffb04d4b56c1c9db76a4e8b7bc8b"},"112616686":{"actor_sha256":"f3640659ef9c55f79d70599a22b07a0082ab062186eea240e798e9f38da512c4","cache_sha256":"9f2cd9dcb62ac63d3ec978e88c7871c8e242f26fe4c04498eed65e1269289601","label":"AR\\u9ea6\\u7530\\uff08\\u5185\\u90e8\\u573a\\u666f\\uff09","level_id":112616686,"netversion_u16":[46117,49307,23536,0,0,25536],"placement_sha256":"050400ea266be1dfc38152091bfd6385671cee57d524d7ea84925d640831aac1","resource_revision":"apk-v75506-421589","scene":"AR_WheatField","scene_provenance_sha256":"219b3a73960e44efa362b4d7af906b3036bf621c14e0bf6ab8073bc48a2a94f7","scene_sha256":"5e4d6ba4b1182488bad9ebfd870ab6441112c9e7ae255aac59a7013793e14d8e"},"1147491976":{"actor_sha256":"5fab02035c4137c94aa1c094ef1c5ebbe13996b13049c49da3a5d1f8f4b8e76f","cache_sha256":"5e27429c7064880618c022865bb7125e3744efef10717ad0b2b1828847c75db3","label":"\\u8fb9\\u9672\\u8352\\u6f20","level_id":1147491976,"netversion_u16":[4310,49307,23536,0,0,25536],"placement_sha256":"e875f83957a6098b6c1189a331bb17ded086db69699bd46911be3f24362cf34e","resource_revision":"apk-v75506-421589","scene":"Dusk","scene_provenance_sha256":"cb3544fd8c181f8539a6f5f7883f08b62225084d30fc6bc488aa68e55b40bdd5","scene_sha256":"828cfa91d55a54a77ae00f965730c279778edeb5673a23f8f34ebbbf336e35ea"},"1152668600":{"actor_sha256":"971c6bf75a685198500ea6b9eec1fcfe2c095beb7b265fb87c582863b5064e03","cache_sha256":"cb5cfe9c7ff99ca3fc463f8e99cc26d42541cf75742a56327a54b525aefa5333","label":"\\u8352\\u5e9f\\u795e\\u6bbf","level_id":1152668600,"netversion_u16":[22572,49307,23536,0,0,25536],"placement_sha256":"4813f16228ea3b6eeddcf77b587af2889ea2a050d1dec38f1b9e12e68adb2601","resource_revision":"apk-v75506-421589","scene":"Dusk_HiddenTemple","scene_provenance_sha256":"3e27ad696c2e5015beafa27436184978d3610999927ad7618b0885eb668dc0b0","scene_sha256":"99f5506292ea6a98003957935ae88c9455bbfb0740592efe7626898a70063bbc"},"1185708672":{"actor_sha256":"acc3db0ceda9c6ae310935555daa455141810c36ab1469c65710c1b10130221c","cache_sha256":"330c5461876e0ed689accdd18c8c980e2ab285580c32b408b8788c41d0efa9f0","label":"\\u59c6\\u660e\\u5c4b\\uff08\\u5929\\u7a7a\\u573a\\u666f\\uff09","level_id":1185708672,"netversion_u16":[61885,49307,23536,0,0,25536],"placement_sha256":"631b3f0b5dca5dc1ff0995d72fa7e26a4451b565d602c6c1e4defef04a7376bf","resource_revision":"apk-v75506-421589","scene":"Night_ValleyHouseSky","scene_provenance_sha256":"d4d7849dd8d5d7dcbf7dd8aebb8954dadc1c6bb3578e68f31ff0ac09df2411c2","scene_sha256":"17f5a6071ac9aa3be472617873ae2f9b45256248187f7a277ff5da33d0dd0510"},"1190972738":{"actor_sha256":"747e6ef54ea8fe610db853eeb34c59c7b26ca93fe728f7acb52e18859423b5a4","cache_sha256":"ac56f15a1036aa064793e1db7db5e652039b63ce72258fb6b1b5ef80db343aa3","label":"\\u4e91\\u91ce\\u795e\\u6bbf","level_id":1190972738,"netversion_u16":[12644,49307,23536,0,0,25536],"placement_sha256":"22a0bd82d2e51e30f845b8e45c4db0b4547c8a80f4ec5c4ed740a24c242e15e9","resource_revision":"apk-v75506-421589","scene":"DayEnd","scene_provenance_sha256":"8a5c55083ed8095dfc65fec2cf98382d0dfc90f701f3507427b69a0fd355ed06","scene_sha256":"fd3409c29a9d2713f26a9311c47ed093fb574311a4c15100834c8d2af21875fc"},"1241316521":{"actor_sha256":"930ae170e2a7e2b2297c838cc5fcef90411129b2449805004cd28c144dde48e6","cache_sha256":"f60e1e6c9944ebd96c473ab5a3df36eb418a4c51ef2d0fc2c2d627e8b7216cb9","label":"Dawn_TrialsFire","level_id":1241316521,"netversion_u16":[41480,49307,23536,0,0,25536],"placement_sha256":"ab6b2e87e36c5454ca08e5700d69b7cd9c26ca7e55302eb1846d4bfa699dfd16","resource_revision":"apk-v75506-421589","scene":"Dawn_TrialsFire","scene_provenance_sha256":"cf2072ebed951c3df033bdba80d83b2f9e155546663d2be6aa5b942c015117cd","scene_sha256":"a72fe6745d81d6bf5c3c00c8f7fe1730d8c4a76ecad4c10b582ae777a2c4c767"},"1274288158":{"actor_sha256":"d7dc04ab7c3acc13986e869dadd576f5fd15152746b6483a5e7e08054ca1924b","cache_sha256":"58424d9c16683914f16234d8cfb412e3701d3b066c43d19a3ec180969b1cee45","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\u4e3b\\u9898\\u5c0f\\u5c4b","level_id":1274288158,"netversion_u16":[37837,49307,23536,0,0,25536],"placement_sha256":"f2a2ee891442f60506575b266b10c119bd0be243757485d82d715c1e4bb6e757","resource_revision":"apk-v75506-421589","scene":"Nest_Night_GO_Town","scene_provenance_sha256":"0db04a6564946c95c4560a0eb4d573225ce6767fe22b692b4924864c8376c084","scene_sha256":"72b800f85320e538d4fb3d31a46eb54850c5f516d9538d7fb985e3c06ea7eaf6"},"128844448":{"actor_sha256":"9ead505f706f1f5e0ca9d05dc9ba6a1dc18b07729357028eb3b86c934ff0e7fa","cache_sha256":"17f1df3f937ebf45bed7c5eb7a8bd022d02d3a995ebc7729b05ec26c0551878e","label":"\\u96e8\\u6797\\u795e\\u6bbf","level_id":128844448,"netversion_u16":[6082,49307,23536,0,0,25536],"placement_sha256":"51a391fcc590ab37c4d7bca0eab7fcddee3a019ef56a8defd8453ad3a8fa5d91","resource_revision":"apk-v75506-421589","scene":"RainEnd","scene_provenance_sha256":"97a64678d3a32c3b1cea20f5647d351ae3af9d4279892ea902c6441b88e56bcc","scene_sha256":"66e4cb35e0ab90b56e7730793f3b9c07cf1f7e1b637c2c3209e66760ee916fc5"},"1323747701":{"actor_sha256":"b20b9181c869f7e443c904598612fa9f5f97facb1ae174a578a07449a4684903","cache_sha256":"a9c3f37916a2f7ddafbc51e22a57eac2e020203f560173daff88710a8415e927","label":"\\u97f3\\u4e50\\u5927\\u5385","level_id":1323747701,"netversion_u16":[45743,49307,23536,0,0,25536],"placement_sha256":"82242592d813d27a2f66988d76382ad5c4186d2fab583049e1847be6cc22f677","resource_revision":"apk-v75506-421589","scene":"SunsetVillage_MusicShop","scene_provenance_sha256":"e8649d7418b06ace809083677560223c734679484a22fd77f1cec8ff7a9c6daf","scene_sha256":"e2b1ea6750bf66435f085cc95672800750633fa9a4b86d5fbf5ed79df8c03364"},"1444818677":{"actor_sha256":"0c76ff3e37af2b03c2c61ad57fc26f9ecc299fa246ea43b26d633e27e50c4c85","cache_sha256":"51b7e05b5fbbf10f08bf9385daa54fac528f6e7e01b42a38401131e1bd0ed82f","label":"\\u751c\\u70b9\\u5de5\\u574a","level_id":1444818677,"netversion_u16":[17330,49307,23536,0,0,25536],"placement_sha256":"5aaf11accd4047f39b60a2bceba9f7ad3111fd277317ade89986a1dd30e4f5af","resource_revision":"apk-v75506-421589","scene":"MainStreet_Cafe","scene_provenance_sha256":"39d1b8f445cb19f054d1c06d65416b50428f4fd3ff87c3cdb4aab65bafa3e623","scene_sha256":"eeb541a68ffc970e7c5358c3841d68b37529c1cbaba99a0f4f54842d4af8b0b1"},"1482972144":{"actor_sha256":"e92271679ef720fe3365070d57e0e391f1a472f4793f85b5da041aa34842768a","cache_sha256":"0f1c0bdf5406ba6f66f2f3e00349c4ae00298d80d9864d8da5b7f10836a94c0c","label":"\\u5171\\u73a9\\u7a7a\\u95f4","level_id":1482972144,"netversion_u16":[2393,49307,23536,0,0,25536],"placement_sha256":"8902d94ef908c81b11cdf4ea27d93a2f6348709c2ce56f0e07cf60409d81576a","resource_revision":"apk-v75506-421589","scene":"VoidSharedSpace","scene_provenance_sha256":"ce09d3ef9f2404de04194bd7c3e6056790a36c92c531d5354a9c56ed8ab8333d","scene_sha256":"f92b4179b7d801036f14cb5822d6b1715c4cdfc1a9d855b31e11a5d62c473f59"},"1509719447":{"actor_sha256":"bd3061d2d139533783029b6f5fab8a39be573609f0512312093e51c69ecb1ecd","cache_sha256":"3e91e3827a632f6eb4468a61d15159954bbd02107d9c89231f5e048e01db0cd4","label":"\\u5c0f\\u9547","level_id":1509719447,"netversion_u16":[1568,49307,23536,0,0,25536],"placement_sha256":"9bd19b7314fb4a473349445a14e4caaa2c5b7280520c8fee3037e44740b4acec","resource_revision":"apk-v75506-421589","scene":"MainStreet","scene_provenance_sha256":"98257b4f17e5732a274cf2ebc4a49b818e5ed59e059cbcdf6eff03058104dc19","scene_sha256":"3375f7f74ff7fb2ac6e0a626815fdcfe00dfd4586b0b696e87d1dbbfd68433ce"},"1559613961":{"actor_sha256":"8fb57c8b4b3dfa298fb7573ddbe4a95914c0057f078e0c30b9149f70fd3448d8","cache_sha256":"60815e767715e5cdb198972ffdefc379554e36ddebe5e5a195d253357bd3c0a7","label":"\\u97f3\\u4e50\\u9910\\u5385","level_id":1559613961,"netversion_u16":[26450,49307,23536,0,0,25536],"placement_sha256":"d6dff95e756693d5a847fb366734dbafbcce2fecc22640e31557a56c58f4b0f2","resource_revision":"apk-v75506-421589","scene":"MainStreet_ConcertHall","scene_provenance_sha256":"81c23a357468a5713550811e7e443983f20bc572a6209d79fcf3de00eace6d0c","scene_sha256":"83d98767360544f1667b767ad9ea15fd382685445f9de7fcff79368a013e3e2e"},"1561318939":{"actor_sha256":"083c22d18542a249ab06f0af3609ae150879dd1ca2eb51a376c2bdb8e4a6c790","cache_sha256":"c25f169a8ecd4a62ea0a1fadc3bdc72ca9d94657722a0978f9482cf82e4d47a0","label":"\\u7e41\\u534e\\u738b\\u90fd","level_id":1561318939,"netversion_u16":[22227,49307,23536,0,0,25536],"placement_sha256":"b33c0768b22ced3d75b87ef16143c299be397b3583427893b1a5ccf97de03316","resource_revision":"apk-v75506-421589","scene":"DuskMid_Past","scene_provenance_sha256":"8275f8e5fdc5d14734271ed87736032eac41999faed1d26c689af961518dc70c","scene_sha256":"760f30ba0909a7ff390f2594805d32f9beb7d4adf3d6b9b37d00c628afc2b776"},"1563891948":{"actor_sha256":"5c4c4a1d5b0a2fadfe107debc259616ff5746ba697c8adb98c036f51b9633f75","cache_sha256":"3803f86512bcb2d7943c2c602014fd00ce95182f287c30b3bf72486c0653eaeb","label":"\\u670d\\u88c5\\u5e97","level_id":1563891948,"netversion_u16":[63793,49307,23536,0,0,25536],"placement_sha256":"58305df7f0f93fe04904c773e2b3b376f8157213da8334bc63bc5f392c9c58e4","resource_revision":"apk-v75506-421589","scene":"MainStreet_ShopOutfits","scene_provenance_sha256":"a6aa2a8a006695be8977559c09bcae62dbc3c42daefdbfeebcbc18e4be7afce5","scene_sha256":"b39460c44a5cea7f92f81db08865dec4b18eeaed6337c59895163cd98ae44a02"},"1597085778":{"actor_sha256":"af59fb0ad24aacbc167c08b0e60908249806b5db20151083adcd9d5971bd0523","cache_sha256":"51929e072b343b3c36c707fa5983b836509e6c456296ccf39fded1243bfc62f8","label":"\\u8fdc\\u53e4\\u6218\\u573a","level_id":1597085778,"netversion_u16":[64686,49307,23536,0,0,25536],"placement_sha256":"90fab1cafc3197d57a841ec3b55c7ed2fb80e6ea2ab9d71a436542dee008194a","resource_revision":"apk-v75506-421589","scene":"DuskMid","scene_provenance_sha256":"d09c341c303fa0062c64369ab3b175c3b6f6b9a1a550a40056bb28f7f59e4609","scene_sha256":"daab0834d72716ee27415763aaf25b5c3f82232ef387f9d8c635b4ca8dd2fa56"},"1612139937":{"actor_sha256":"76d0b2f0e8b6fab0a0d6c83b2e2dbc5001466cd910720eef8edfed3d33752c96","cache_sha256":"0e67d82f406a00c820e4148ff2bef2dc2f88928e69f552ea7da8cee0764d11ef","label":"\\u5706\\u68a6\\u6751\\u5267\\u573a","level_id":1612139937,"netversion_u16":[56922,49307,23536,0,0,25536],"placement_sha256":"e068129685748f9a0abc972c265263f40d34ce47fab943f2afdd8a7a6828e9c0","resource_revision":"apk-v75506-421589","scene":"Sunset_Theater","scene_provenance_sha256":"323e40cdff8e5decb964042b074102fbe177ef62081d6ead6e85feea3e7a68f7","scene_sha256":"8ccf4dceec9195ea8bfee1afaec31d6f3623a25bcd5e107678ead5d442532244"},"1638008359":{"actor_sha256":"d2393526caaf56d2a30c0fc11d42f2e6d71fae06ca059d1d91676314edfa9c53","cache_sha256":"f853dd0e293129049859dab2974ed9f9327b651ed1d77fcb82b29a807a1ab7bd","label":"\\u971e\\u8c37","level_id":1638008359,"netversion_u16":[50293,49307,23536,0,0,25536],"placement_sha256":"b5276dd0227493c00924c6ec2ff950a661603b212a361172213c183aa1fd95b9","resource_revision":"apk-v75506-421589","scene":"Sunset","scene_provenance_sha256":"3f72b7aa985bd0ccdb200845d4152a08e6189f5a7b1c38aecc39363cb37347de","scene_sha256":"d0f219ad76207fd6a129de3d5bb82b78648e2237c8888d34ef80cef6e3150c1e"},"164626931":{"actor_sha256":"6a33b5ddfa027e165a1b73a40fc6a108859aa96e81c2930e90d2b0e2f47b4e08","cache_sha256":"2fc57960e52151b78046647532fe0bbdbe7d7df07dbd509222f80afc2b58eb72","label":"\\u96e8\\u6797","level_id":164626931,"netversion_u16":[44931,49307,23536,0,0,25536],"placement_sha256":"900f2fe26e31c75c2610df59b42f4fd45f155329e647bbda62ed535b5cb0f821","resource_revision":"apk-v75506-421589","scene":"Rain","scene_provenance_sha256":"fd79269db1c758188160b0d1e802bd81a86947ab4f6ac8d3e47c9e9eb9201eb0","scene_sha256":"00a2f95c495b837ccf36df9d0cb8d8974088417553c2ef8e86dfe81aebeb803b"},"1649439303":{"actor_sha256":"c6cb564441feeb5f903cecc059895767ed4af31b52d94dbdf7f6239113d4fd06","cache_sha256":"1fa657c4a229e51ab2944de02205f8c87865ec14da85fafb0c92a1c4ac11e707","label":"\\u6668\\u5c9b","level_id":1649439303,"netversion_u16":[45075,49307,23536,0,0,25536],"placement_sha256":"399383521904b1ab65dfacc8d12e3c0c1d497c30a0ca261e2301bff68e95fd68","resource_revision":"apk-v75506-421589","scene":"Dawn","scene_provenance_sha256":"28e529616dbef5a33d9976d1d22655c6b0ff0b20a01f221dd8807a6e9c0c7a7a","scene_sha256":"3b7f9a7daa6a4fae9963187cb4957ccf4163cf571e16cdb695fc2e5bf8958189"},"1661095040":{"actor_sha256":"34433dbae387c6bb76d7f2827242d7aa3f70ad51fbace7e502171c828b1cfeb2","cache_sha256":"f78be25ba1fb2e73192a56a6ce62db921f433a2ecccfb084f4b7a5a2ea0757d2","label":"Event_Arr_SoftInside","level_id":1661095040,"netversion_u16":[3991,49307,23536,0,0,25536],"placement_sha256":"bce3b842cb0c8194fa5d76df5dd64472ebbd3e4565352a6250db0c85105258ef","resource_revision":"apk-v75506-421589","scene":"Event_Arr_SoftInside","scene_provenance_sha256":"9ed46ddd629da548f1fca2d41697dfbf020413a399b29773e5e707cf8ac9c070","scene_sha256":"acbca7cde3063f4274a849dd1870cfe2a1caffb4b12a3b7978d087183e483ae6"},"1665365959":{"actor_sha256":"9f1e1bf47d96f6860c4826caba537eac915e64dc4e4011d2069b4fc8aa7fb66c","cache_sha256":"154139d6e3621ac4fb0231eb924fce40c7b2cb517450b44df2307faeaae2b226","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\uff08\\u7530\\u91ce\\u573a\\u666f\\uff09","level_id":1665365959,"netversion_u16":[46218,49307,23536,0,0,25536],"placement_sha256":"63c74fae0d3bc950f19954bd3cc99f0a7baa8123aa9a970e87ed2a0ce6abd28a","resource_revision":"apk-v75506-421589","scene":"Night_GO_Field","scene_provenance_sha256":"ea08f38796b2cbd18d3de0106994ef543196b2994c966bb5377eeb2143971fa3","scene_sha256":"694ad5f4d3e21309183c36251ff2bbde9756f45dd3027bd487d990a493e553e6"},"1694829989":{"actor_sha256":"a484e12ce0744949bff2db68efe2e35df8ee658e1665f2feac3cc16893bb9874","cache_sha256":"ae3cf0470682c8ba02cb3059ba3beb302d57d371a6bd12655af1285f818973dc","label":"Night_PaintedWorld","level_id":1694829989,"netversion_u16":[49660,49307,23536,0,0,25536],"placement_sha256":"0292fe0a8f71121dd4695be28ff46e4f0eb6c7a3ad380a2c53d7bb1b4f531dd2","resource_revision":"apk-v75506-421589","scene":"Night_PaintedWorld","scene_provenance_sha256":"6219d1b306936c0adb253eac08534030be88cc0ffc178eb2e0ef4b4434f063ad","scene_sha256":"f68732fefd74f068afe00ff894e4d43f4c81e89693187899347a8694561d294d"},"1705189686":{"actor_sha256":"71bf16796c5c2b54cb771747391d243ff042148b42e4dd60fb73296aaace389b","cache_sha256":"cfc7b14281508bd09983a8a7b5c9ca53480781375c8791f66f97657cda635625","label":"Storm","level_id":1705189686,"netversion_u16":[24346,49307,23536,0,0,25536],"placement_sha256":"4f764bd1097c461907de2b3e8b372d64104c4e306155a0e403428592db73bffa","resource_revision":"apk-v75506-421589","scene":"Storm","scene_provenance_sha256":"71e01ddc485220dbe3fe834d3e96607c72d0e6f6e9823cf43ce8220e803dda6c","scene_sha256":"8cba8ead4204fdb11755088d49117d38bd4929b69e055421e843a8b3139c82ae"},"170656205":{"actor_sha256":"eab5067b31144a35f11bc72cc07c88dd684922d6c3d2e448502be111ee07d109","cache_sha256":"2852694ae47a8a6fe485bd93d85313308a422860428eb138eac756f8be0024d9","label":"DuskOasis","level_id":170656205,"netversion_u16":[17784,49307,23536,0,0,25536],"placement_sha256":"edb3d51d10f21e74f5e1c8c23415ae2bb12bdc040120c3418c731bc02dcb79ff","resource_revision":"apk-v75506-421589","scene":"DuskOasis","scene_provenance_sha256":"630978747216497692cf0dec8ed91fbb336ca7c711d6205144863c9fd11337cc","scene_sha256":"c66d858c549d8dc028d7ce900c7088a92e7849d9d15cf29294647ca60b44d5f5"},"1737684623":{"actor_sha256":"a2fabcd93f68eef1697bab88e0e63531262911165adb25adbd4aa6ddf929a776","cache_sha256":"41071029483a11ac9ee941c89016d9c1e02ac774ee5a847be2d143357b9b6d8b","label":"\\u6c38\\u591c\\u4e4b\\u5730","level_id":1737684623,"netversion_u16":[61909,49307,23536,0,0,25536],"placement_sha256":"b1f0b3c560ebd49dcefc84568224dbb6d3efcef9346565284328a78c1536caef","resource_revision":"apk-v75506-421589","scene":"Event_WerewolfGame","scene_provenance_sha256":"1541bf4845b717eca76d410190354716a6cb3ce81274a0627aecab62dea7c1be","scene_sha256":"30e3b7ccea9658ddaa51ee23279c6493ac39fa91b6919cb2cf711fcaeff81d6d"},"1759178769":{"actor_sha256":"2b89a4480b519dccb85462dd1fdd53e347b0f312f8da223fc43239fe7b397583","cache_sha256":"d5f76da92cbae9bfc5c76b39abaf35f5ef27fcc8cddae35303ad350db8cd7825","label":"\\u5706\\u68a6\\u6751","level_id":1759178769,"netversion_u16":[17529,49307,23536,0,0,25536],"placement_sha256":"7c38c3eb11a70e964be88c95b6735d596994c6d7d9d8d392ca3ffa007d94b4d2","resource_revision":"apk-v75506-421589","scene":"SunsetVillage","scene_provenance_sha256":"78d0808f1a03c033b7185c5dcd5e44ac11ae5a1553f1458f0472741072d593a4","scene_sha256":"00344e96c66abf6824359f662f3441bca8716760cd0b125045f99831baaff1da"},"1761965310":{"actor_sha256":"664e21be0260095d4ccff113bcbff1e583ea195e9ed9cae880425fb18ed0787d","cache_sha256":"ea8e99b2a832fbe197b234ec20d36eecfd4efa4b08be708d7efd2c10de01b63d","label":"MainStreetFlyingIntro","level_id":1761965310,"netversion_u16":[62514,49307,23536,0,0,25536],"placement_sha256":"2b2b6d944bf2201bbb0291ea3affb7640c247cd466fc0c9716999f322ca48233","resource_revision":"apk-v75506-421589","scene":"MainStreetFlyingIntro","scene_provenance_sha256":"9bb65b81c59afcee73d4f9c2f530bcaee6eebb98c200460b1f647fb6b5095315","scene_sha256":"a9e13587d8d78454df274be8e4f7c3b151d2b4d66dca084034586a8c9f6b14a8"},"1801452336":{"actor_sha256":"8fb112fb4b384cc459d61bb1767195d74997f9968a0b0a61928a4bbb2b74f65a","cache_sha256":"dc2e28bd03b45cf02da7ad78307e8d7fe5ca9fddc87da74bd9c1cf3549ed4690","label":"Night_ValleyForest","level_id":1801452336,"netversion_u16":[40183,49307,23536,0,0,25536],"placement_sha256":"5d609dc153e411757473d82c1f5649006d24beb606e81e6e20643ee28a1ec518","resource_revision":"apk-v75506-421589","scene":"Night_ValleyForest","scene_provenance_sha256":"cf92e431d02235cec5b56793b5dc20dfe85be549a02f40896a1e0d9ba43a6aa5","scene_sha256":"bc25b14641698fbbe67d75f1867d7f6b6f0ed7f5b8f13ec0b9bbad4045ddccd3"},"1844499196":{"actor_sha256":"83e5cd62b010972d7398535740ffc7b35a733850c2bc1ba467836cb326d35d43","cache_sha256":"c2d22b3e8c94a81d0963e6716432ce6f9e5f41ec7cb276bad19cb0dd0495e4a6","label":"\\u98de\\u884c\\u8d5b\\u9053","level_id":1844499196,"netversion_u16":[30151,49307,23536,0,0,25536],"placement_sha256":"9714f2c1c92a6bb614a512dbab099c6a47430f491e29bd201731e91386b76ff0","resource_revision":"apk-v75506-421589","scene":"Sunset_FlyRace","scene_provenance_sha256":"da12ebe75a539a722936395e500bde4797288987c6abf3799cfca533cd1ce246","scene_sha256":"a48033f78a44a872a45201e6e6125179e8e865013fe5b31a72bf8298a448cc29"},"1887730855":{"actor_sha256":"3cc3124c53da81673772d32e12dde465702f49bf6d04a8170d4542cd58fc94f8","cache_sha256":"f6f6c2552b006f1a29fc3387f166ee443f64c0cf43b5d905bb9996738577ecaf","label":"Dawn_TrialsEarth","level_id":1887730855,"netversion_u16":[47323,49307,23536,0,0,25536],"placement_sha256":"a7b41f25ab34811bf9c36b4d61597339066822854bcc8ce4f843d5ae2e9708c0","resource_revision":"apk-v75506-421589","scene":"Dawn_TrialsEarth","scene_provenance_sha256":"7e86dd105eb51dbef04de041e960cb210095914908c00e34156bd4d9ac47a1e9","scene_sha256":"3dab9d816606f62ffd0980c19b9eb040aa0677c3104dac192b9605a557cd8c7a"},"1936276829":{"actor_sha256":"4cf7de3b5061a551537cfc70123e6af8d767759c0342a7eded54b7401dc2fcf3","cache_sha256":"c37fd0fc253bc1576c9361af3239d1cf331b83969566e7c02c6f0aca5f2d0f7e","label":"\\u6545\\u4e8b\\u7a7a\\u95f4","level_id":1936276829,"netversion_u16":[46559,49307,23536,0,0,25536],"placement_sha256":"70fd65b42466dfc0744b683d0ec62c14eb4f9d699a4bd84a0cabbdccbe94e127","resource_revision":"apk-v75506-421589","scene":"MainStreet_StorySpace","scene_provenance_sha256":"57d274fc3df0df1fd59e61366506b1eebd21a5e97512af88ef9e8c587f0066bd","scene_sha256":"8381845a24a5c56a0fc032d1e769b3aee566a567787aecaa06d5317d36ab5b4f"},"2050064391":{"actor_sha256":"99cd3ff4d2a15dc1cf90aa4752f90ea58e8b4ef546c82915d50a78274493a142","cache_sha256":"8ad42b3674593e6bbaf29b45fdca8f47a681f64d8d567260b65213da43669c6f","label":"Dawn_TrialsAir","level_id":2050064391,"netversion_u16":[8243,49307,23536,0,0,25536],"placement_sha256":"b21120c3230bab5694f48d4bcf8f0039ebf2bc26abf2b257fcc5c8a1c8923313","resource_revision":"apk-v75506-421589","scene":"Dawn_TrialsAir","scene_provenance_sha256":"580d0430a73e26909724a7e4fb629f860bf1c465b4c8264cd28eae40c93438ea","scene_sha256":"21756a1f1a64c282b79cb8cc0b7d48ba885716fcffdbc6d43e3783be8501b289"},"2060214456":{"actor_sha256":"5a61eeb8ddd5266bbc4af05ce2f1b7bc3679a18537e7ec2d2ece076870dec708","cache_sha256":"17472ca9a45198706fc140e977970e784ca8b8e0b938e5213cc51ca96784ad51","label":"\\u661f\\u5149\\u6c99\\u6f20\\uff08\\u4e3b\\u573a\\u666f\\uff09","level_id":2060214456,"netversion_u16":[61447,49307,23536,0,0,25536],"placement_sha256":"25d136e4e629d5f9b44280dccbfa83722746299e0ce84ba7c47affbd640b84de","resource_revision":"apk-v75506-421589","scene":"NightDesert","scene_provenance_sha256":"5ef3ea5e3cf26e4626eb8011d3a59f393b3fbd0f08eb6fc5201467d17f149195","scene_sha256":"2ae87962c0f1e3db0abe071920107b7448f3bb37ac9c321338680ed622ff9b7e"},"2081768701":{"actor_sha256":"ff18f91d589e8e4ce67d56bcc3647e471be70a547601594c3b8af817127e6c57","cache_sha256":"e758ab9c26c1a8d5cf3811e334d6530d29fc37a55aecbdf2112e76ca241dd96e","label":"Skyway","level_id":2081768701,"netversion_u16":[39846,49307,23536,0,0,25536],"placement_sha256":"1514b9ffda0c58da5296057e7eb240cba7d2f417a6f5eb52733b1c8c548066c8","resource_revision":"apk-v75506-421589","scene":"Skyway","scene_provenance_sha256":"0d2d0f14e16c38f517d7d9c51f7d5bab07eb4103479a5f6df48cce8923d0fb7d","scene_sha256":"60427d78aa746e088ea02d6cc69023f174ba9f5cd7b02f020fe835c50758a54c"},"2093785354":{"actor_sha256":"3e1cfe3315963d64eb36d3522da8f003c5e93f091de1335c210a989e8537d3a4","cache_sha256":"a0a45f314bfc5244920c33c703201721b4cf99139163c54c2b5a46bdd18a34f8","label":"Event_Arr_TheSeed","level_id":2093785354,"netversion_u16":[41045,49307,23536,0,0,25536],"placement_sha256":"0fc1bc02e61a7bf13489f74e5f4c977658b30f991eda5742e9b5572e15566a0f","resource_revision":"apk-v75506-421589","scene":"Event_Arr_TheSeed","scene_provenance_sha256":"15bcf0b08b7fccf7c02859fc35e9ce9c6b64bb0a27bc4079bb2a4ce84c1beb2d","scene_sha256":"07ab68dae47d8cb07f610a9d02802226b26c68a3a777697c634f820102699414"},"214074919":{"actor_sha256":"302c4f97a846052331a09c4b4450c4ddc7f8e94b04821656d5bdb667f93bae96","cache_sha256":"6dc90352625656daa60ca8e5bf786891fea5021ab9a96db7a11b9a9794093655","label":"Night_InfiniteDesert","level_id":214074919,"netversion_u16":[34451,49307,23536,0,0,25536],"placement_sha256":"1ba36a4e037a7290e0e70457fcf2e746973c3f4cea699ef792c6f63ac59027e7","resource_revision":"apk-v75506-421589","scene":"Night_InfiniteDesert","scene_provenance_sha256":"1753ed3e6a224a246a9ce14e5283a9a2f395e5ca1e783e7c00f71fcd37dc5979","scene_sha256":"9506f459eb847ebbeea7137e5e11bbcfb1eee9f43487d7d20aa341b487d3c236"},"2159642775":{"actor_sha256":"7a497fefbc28c2cecd10f10357d8eccf0077baf46135ab1d7fa9aeb64cd12a90","cache_sha256":"5eb39f64723869f2d31dcf8fd5b75f79aeba07fb30ceb61d8d727663ca2a41fd","label":"\\u5bc6\\u6797\\u9057\\u8ff9","level_id":2159642775,"netversion_u16":[25546,49307,23536,0,0,25536],"placement_sha256":"960352bd12b6725051018093911cff2c102fb16f42ac5a516e812af8f5d4fd16","resource_revision":"apk-v75506-421589","scene":"RainMid","scene_provenance_sha256":"32f0386d295be1e4c021c3c9d8aef0d188ada54a4a6bdf4661b91e14107db1b9","scene_sha256":"7091d279f7084bb156db40416f0c91fa26aced2170d3417f388c4eee210fe076"},"217257707":{"actor_sha256":"1c57baa063c063cd28b1c6c23a952ce48c9b902eb25938dc2c3544f5a3531ea4","cache_sha256":"68cd667126f83593636a1fdc8c82583f22310ce86f0ff6afc7e56acd2d39d7d8","label":"Night_ValleyHouse","level_id":217257707,"netversion_u16":[51358,49307,23536,0,0,25536],"placement_sha256":"35a8462fcfb352e9145ed14ca78821a5e0ecdfea3616eee20264b49b22664e0b","resource_revision":"apk-v75506-421589","scene":"Night_ValleyHouse","scene_provenance_sha256":"eeebeff2a7c20026d7c27dc05e8182ef1f9ba716a7833ee03f31d97360e854d6","scene_sha256":"f89a6352236added05b0be19a3dee5e5a035a3aa79860f1ea6ae2a6423510b63"},"2179549040":{"actor_sha256":"e4006ab5dfd3e77d7baafa36b46ebc96f0d60ae9fc5361d9f380e5f932d1d2cc","cache_sha256":"980ff593563c7bfd0978559bcfc2089c80a1cb85947c63c71bc679f09317b265","label":"\\u971e\\u5149\\u57ce","level_id":2179549040,"netversion_u16":[37851,49307,23536,0,0,25536],"placement_sha256":"f7bd402080884d44dc58c78bf12c5c99bd99d98ec9a0b5b4c7d382c3d0ed10ca","resource_revision":"apk-v75506-421589","scene":"Sunset_Citadel","scene_provenance_sha256":"4228e34ba17ab0479ad342f51fc14ffe3ed33191b8a57c343cccff786fc65fe1","scene_sha256":"b76e6953c16daad71fd7d6f38e933e0590d97480245b283305252298de15c6e8"},"2199862534":{"actor_sha256":"a1642c0117ccaa946ad0361181fc40a4646fa8364b38e5cf6cd708038b174b7e","cache_sha256":"2dd9238f0a71520114e062a37208054fd8bc7eb2d6dfad6cb039d2b02f8f6860","label":"\\u9690\\u85cf\\u5173\\u5361","level_id":2199862534,"netversion_u16":[44969,49307,23536,0,0,25536],"placement_sha256":"73f5553c01b8effe4bea9d3f1c933a3515875cf90ecd7f484d11631574281572","resource_revision":"apk-v75506-421589","scene":"Nintendo_CandleSpace","scene_provenance_sha256":"c077c85971e6d69bf932a8836dc87ed8457d835f69c28ba66c57a57bad14bb6d","scene_sha256":"5134b64ce8fef5ff2c4b4573232feacd213bf8103d9e1bf9e7fca690725bf863"},"2251284635":{"actor_sha256":"b1d4b71c6b613269c92a262aa835263ab948c48d67793b99d3edc4a9b2f86e42","cache_sha256":"ea658c2f946c70c47e7b7166ec13ad491d1db51be3746ae7a6918c1a90e0d0a1","label":"\\u661f\\u8f68","level_id":2251284635,"netversion_u16":[51912,49307,23536,0,0,25536],"placement_sha256":"6a9f5735a19a1346a53f7ce1da821e8356f8bdaa2d154d91a5abd93795ff42be","resource_revision":"apk-v75506-421589","scene":"CandleSpaceEnd","scene_provenance_sha256":"1d1c16d911e7f1f0d2ed9deaaee1ca012c793087c48b9be66d544e3fe63da10d","scene_sha256":"65634ef68a93a5d6df1a7b1d06cf873fa3e0c6929d33d7bff8b9901673582b8d"},"2267185542":{"actor_sha256":"c4d68601fef77805561cc0aa2ba9f8a7e4cd7c34369b43219d1c3869831f7137","cache_sha256":"8b48c5abd73c85b9c3670fc0041b4a25e37ccc85eed8b8552d978557e68fedda","label":"NightEnd","level_id":2267185542,"netversion_u16":[31079,49307,23536,0,0,25536],"placement_sha256":"32f2b69d4740bf95f1c198e10ac01912cc020064210d0aeb1dd7304793ec037d","resource_revision":"apk-v75506-421589","scene":"NightEnd","scene_provenance_sha256":"c382b16228de616a3e078098c6a7e756b131b449d9425f135adda06848fe49d3","scene_sha256":"591ff132e2dab9a297c6b4ed883dc8d071fbf3b514188aff820db8c3a1015572"},"2307461961":{"actor_sha256":"3f8e382e77cfe117e7a066ba5a975503c38cb01bafbbb3648c10d67fa327bf21","cache_sha256":"41650d5ba3a10af259766175a33c8b63deb76c5164bdcec4c6fca71f5783d4f8","label":"Night2","level_id":2307461961,"netversion_u16":[41630,49307,23536,0,0,25536],"placement_sha256":"d5f6110b32c3b12b8bbd4d9ca5f3f10b7fbb88c9f426d50f1db3f0a86a60c4e7","resource_revision":"apk-v75506-421589","scene":"Night2","scene_provenance_sha256":"3743705a4a6fcc9fd9d5f26f2c71ac382a2805df8daa7836b2ed9688dcafd5d2","scene_sha256":"30a3067c334f44a86bd914666962f7803861eab82c8690df8294d8ce933bd66b"},"2350532176":{"actor_sha256":"fd722ec3a047f5f890e739a07cba569b05bcf013d3b3c51c59e916d32ef56a64","cache_sha256":"981df8d051d8302be1997073e5d6ac4bffd80cc4573b4e29bf81009b933c9e6d","label":"\\u4ed9\\u4e61","level_id":2350532176,"netversion_u16":[16774,49307,23536,0,0,25536],"placement_sha256":"531a05711007b525e32212f93f941454c64949acd38c1a56f010a554ae57e766","resource_revision":"apk-v75506-421589","scene":"Prairie_Village","scene_provenance_sha256":"46b0b7408d3fff7b3fbb34cb434ef1bef32c20d5a7f477e0fca40c8e3a1de21a","scene_sha256":"c5df85d3c8066e4e4aa996f3d7a5c515c282e9dc48e506638bce00798a3b75f4"},"2358907137":{"actor_sha256":"8b0d4634373838b125a82f5ba080be1e932026e3e34d79ff902fd8dea7c0b8c7","cache_sha256":"18d4e2cfbe6ab6fadddfd7ba29a363c69bdae28da44dfe4eaa10de7681cee38a","label":"\\u7981\\u9601\\u5e95\\u5c42","level_id":2358907137,"netversion_u16":[19043,49307,23536,0,0,25536],"placement_sha256":"de3b26ac9cbced011bdc2e659c5a79c8d865803baaed3ea9563f348c9ec56879","resource_revision":"apk-v75506-421589","scene":"Night","scene_provenance_sha256":"8feafe8eef5a2a1f9552d9371d7dfa22ce4025a13d4f0defd6686235a3f048ad","scene_sha256":"46c18c45a1b22172bd14224440137782fac702fdfe9bdeb40f67f17914120e72"},"2360310676":{"actor_sha256":"324421abcf1f37cb3493b3fc4591d7ed4f51940a21c836b0aac5501c15531fb9","cache_sha256":"082fc50120e02c70a99a35de4f6fa485532f6696bf319dde0fc652b832f4a768","label":"\\u843d\\u65e5\\u7ade\\u6280\\u573a\\uff08End\\u573a\\u666f\\uff09","level_id":2360310676,"netversion_u16":[33428,49307,23536,0,0,25536],"placement_sha256":"886431e65bb1788df6c6f9578a1a3badf4d4f694e54c56d2534454f28cd385e3","resource_revision":"apk-v75506-421589","scene":"SunsetEnd","scene_provenance_sha256":"d51ee9b2630007cf557a4a08192af4b6abf1e6a7a409139b2295fa2384bef1c0","scene_sha256":"831b03d7a897facbf88edf0f00ff5af55334c78cf6ca720fd005505e6547850f"},"2394719185":{"actor_sha256":"82834ce5c921c70ab3f790820121ace3377428d4b290f79beefb6f8d21414da0","cache_sha256":"518c7dd3830c310e0bdc6c4f47b658ea0663a99546e8d83f0775b78219884c5c","label":"\\u4e2d\\u592e\\u795e\\u575b","level_id":2394719185,"netversion_u16":[63310,49307,23536,0,0,25536],"placement_sha256":"cdfed7657c1195bd4daec0b1b99986be36228f3e0e82448d9b68321e736b7059","resource_revision":"apk-v75506-421589","scene":"DayHubCave","scene_provenance_sha256":"f22c176fb397ef2cefc4fe51b589ce8740b2ca7d9a5e1295e8164e5556119466","scene_sha256":"83420e648db40f51c9d8de8e6560a6378499d6c4584cf5d0e99fa28a73fdfe27"},"2418094228":{"actor_sha256":"4b14162579707de7c9ba8e3d6873aa7d174a68c3d46f06ac0d3162d3db9263e5","cache_sha256":"1d45658e293285efd0d6e43e0a75aec13ea2b5b590706f7aae15c27d78fe4ff4","label":"\\u6076\\u4f5c\\u5267\\u5bc6\\u5ba4","level_id":2418094228,"netversion_u16":[10763,49307,23536,0,0,25536],"placement_sha256":"d91419930357db6bfafa64320f86e26df3ba5cbd33cefea488c387c8cfbf0c69","resource_revision":"apk-v75506-421589","scene":"Event_DaysOfMischief_Escape","scene_provenance_sha256":"862266a120923c3fe685d0afe0aa964618860bfc4c8d0a97da1e91ea07339925","scene_sha256":"2387df2aa42ded1a46c22864c5f80b3ee566115642d85ba2a6182af3034d508f"},"2458626594":{"actor_sha256":"a29c1c9e6f808b0f2bc3d9bf0cc19e28650948ad1d602f8679a349df46cd4a91","cache_sha256":"a49426725f649cf33ef777273893b7067bfd0cfae2a1fcb068951a5c3a024f7e","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\uff08\\u57ce\\u5e02\\u573a\\u666f\\uff09","level_id":2458626594,"netversion_u16":[17253,49307,23536,0,0,25536],"placement_sha256":"a89bf6d8674a43cc21cf978cbbec40d6a405ee4738be4be3a552f5f01025c562","resource_revision":"apk-v75506-421589","scene":"Night_GO_City","scene_provenance_sha256":"5cc5b6779d64959994ec019633776170cf9648bbe2de03de4c7d5c7101dc5798","scene_sha256":"98a7ccdea0dbc4a976286adbcf0eb7f68e0fb834310b3d69a146bc60bc4d7a87"},"2477345666":{"actor_sha256":"e87dec89e8a1683a79f119bac035786b123d5b7b435ad49d8e5849109aab97ca","cache_sha256":"9076c6bb100b1fc35808f96d8df946ad47009ee0fd13c03d2782b6ecd3832fe0","label":"\\u8774\\u8776\\u5e73\\u539f","level_id":2477345666,"netversion_u16":[42205,49307,23536,0,0,25536],"placement_sha256":"7c4df3bbac3a7ad39353b350bc8d02f90534ea4a11f8c5b216789bff5eb915e6","resource_revision":"apk-v75506-421589","scene":"Prairie_ButterflyFields","scene_provenance_sha256":"3c7204a790f8b5b9beb68f5877915e945aa54a95419f27cedf7464c01a7fc4cb","scene_sha256":"b101d01638dba48272f8c81d35df859cc8ea84be3188becb7a8eae93ee0866e1"},"2518601":{"actor_sha256":"99916c60de406810e07c78e693a1e0726d73475c809c36af26b8cd3c510d7219","cache_sha256":"d247917955ceeabb873334ef79185d1b151b750095f20afbb5c40caeea0a367c","label":"\\u6863\\u6848\\u9601","level_id":2518601,"netversion_u16":[36184,49307,23536,0,0,25536],"placement_sha256":"9ae6ac8d8776acee98cb60e7c123974713f2e891dc422b90262c8a59c0fc167b","resource_revision":"apk-v75506-421589","scene":"NightArchive","scene_provenance_sha256":"c9b5248ddbc2e6ae3caf6c6a9e1d633c5638d007bb0b9f9938622e485107e86b","scene_sha256":"d6a7da1440f83ed70ffcee8599f2b2e35bdfd2effa49118789245186dabb3536"},"2611321369":{"actor_sha256":"b30a8eea1c52a4bd91d6264ef33655bd0e4cd16a4c5aaa6d0ffa7786ac638cff","cache_sha256":"50556e51d2f2bad994c0dc066f70a6911e7420133816e919c8792b49aa5e3032","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\uff08\\u519c\\u573a\\u573a\\u666f\\uff09","level_id":2611321369,"netversion_u16":[27680,49307,23536,0,0,25536],"placement_sha256":"7cf4df4c67e672ec8c6d0e2c386a964eb85a5f9d99cf7caa451e071764c7b2b5","resource_revision":"apk-v75506-421589","scene":"Night_GO_Farm","scene_provenance_sha256":"9aeed29d95f1bc4c20bb5681f1c1bc0e21606c71889aca831a428028826e84fe","scene_sha256":"83b2d2c0df640201c605ed229b471aeadab527811594013db769709eb3145140"},"263580627":{"actor_sha256":"8d2b2fcbf022cd10fc992128f5ace206cfe4aaf3577f51117d5acd2cc53a2f02","cache_sha256":"002a7f9bf6c807550630e804a6bd1a12abd51e0a095629fb1f98e4dd991bd291","label":"\\u843d\\u65e5\\u7ade\\u6280\\u573a\\uff08Colosseum\\u573a\\u666f\\uff09","level_id":263580627,"netversion_u16":[55802,49307,23536,0,0,25536],"placement_sha256":"c1d8c076ba6222e806c8f8cd329ca249dd67c74745acdf9c026b10e49ed8c78d","resource_revision":"apk-v75506-421589","scene":"SunsetColosseum","scene_provenance_sha256":"3849bb9665abe0daf8611060485fb63b641b776ec8cf9a91488f5e86acbaf585","scene_sha256":"5de07440a98db33f095c99056469cd8b364ad102c6fa1efead2c8b648d5dad0d"},"2650921869":{"actor_sha256":"5eaa6355fdeb1dd5661c957e493db1834b980662a78128839225bddefee9a36d","cache_sha256":"851c50d4e8ceaa308bd18348e5e0a1b5c90c536f334f08a831f7081e410532d4","label":"\\u9ed1\\u6c34\\u6e2f\\u6e7e","level_id":2650921869,"netversion_u16":[22429,49307,23536,0,0,25536],"placement_sha256":"4f9c5dc4f7604bf98676c00541e19b438493a9410cb888737f9b6fd9f0d00966","resource_revision":"apk-v75506-421589","scene":"Dusk_CrabField","scene_provenance_sha256":"aa9b0de82bb4ecb64ac05726911f0a1d32a04e8b6f26a61d2ebde043399b1340","scene_sha256":"9d2aee803420b2621c78c5a62967f6319a2e11423d4f444ddbb3adc5f16a5742"},"2720691892":{"actor_sha256":"ea40aab1ca698f00d2962c361db75327d2ef0a731f0370be50e3cfd7c1f27a1f","cache_sha256":"2ee4a95a936e4290264e587e24485cec6b1d9389740d14983b710aeacf04ce3f","label":"\\u79d8\\u5bc6\\u82b1\\u56ed","level_id":2720691892,"netversion_u16":[39931,49307,23536,0,0,25536],"placement_sha256":"5494c80032ea5c938886164552e211ff59c2179920b461784edcb05e6b7c8b25","resource_revision":"apk-v75506-421589","scene":"RainShelter","scene_provenance_sha256":"842bdf065541146800054483c3f67c87747aad46446c5d66f484057fe7524fbe","scene_sha256":"e6c11ba8190a3af1ce47763062a615d4320a66331e4ff3ddc218b17632ad02bf"},"2779731386":{"actor_sha256":"05c7ac5865a52005f38d22c564aa111cb5ae75b7dedf490fbb7d201a4a341d28","cache_sha256":"bf97934ab87f816dcd65fa72b8b829fb8d045e44cc220103638dbc71ac13c888","label":"\\u6b27\\u82e5\\u62c9\\u00b7ExhaleInhale\\u6b4c\\u66f2\\u573a\\u666f","level_id":2779731386,"netversion_u16":[18128,49307,23536,0,0,25536],"placement_sha256":"54da9ec2cb3f157109438d1515f0a703c63921c589498a58ff77651f173b9f09","resource_revision":"apk-v75506-421589","scene":"Event_Arr_ExhaleInhale","scene_provenance_sha256":"8477058d84e6b7d9ea38c1b9eb454224e09e7b3e6f3655a579a4c0537ea73d17","scene_sha256":"eb06df36098d6537b00b79da9909b005b1dc2603343669644fcff8675835c680"},"2824771136":{"actor_sha256":"a57c2270cba0c323de6bc5eb3d71ce26c44b5c2729a178628054dec798ed5f35","cache_sha256":"1cfebe47606f80bf4ea11cc90a7bd60b76bfe69dd52212287cf43931766c6336","label":"NightDesert_Planets","level_id":2824771136,"netversion_u16":[55942,49307,23536,0,0,25536],"placement_sha256":"716adff5322f809d3bb9ac3a9f9dcd599bbcda9fdbfcffc0ff2febea7fa388a0","resource_revision":"apk-v75506-421589","scene":"NightDesert_Planets","scene_provenance_sha256":"516da6bc46dd09e747b094948a8759f5bffc44e5f33829122f29f5e3e0b62ace","scene_sha256":"4cff87849e3942cf6cb2526bc57e8aece4f83693c97590811e9f1f5d25d017e7"},"2839585646":{"actor_sha256":"67b14e1941cfc364cd8c425fcd3ab3ca6fdddd945fe519f8634cf2ead2daeb34","cache_sha256":"6145caf3ef7f629f3b3488fed28439af5d9f59744356e96d646fceef49462a0f","label":"\\u8367\\u5149\\u68ee\\u6797","level_id":2839585646,"netversion_u16":[43870,49307,23536,0,0,25536],"placement_sha256":"76185baa95c1cc50e24e5a4ba32aea398b1f0428e04a79654ac67b73bb3bdb54","resource_revision":"apk-v75506-421589","scene":"RainForest","scene_provenance_sha256":"a8190ae76fb6d921467df837ccecb8acf47bc48b8a4bbd69d90a30fc82719ad6","scene_sha256":"17fe7f8cb0a9642bf8178fb4f5081fe47726bacc59cac5255c75d2dba627f06d"},"2899790755":{"actor_sha256":"f8468924acaf282ff0a0cb30f8ad4238bf3621035efd659772401f43384983c3","cache_sha256":"c6410f4b387f86793c859e5fcec0eb19558166e66baf93e6de4b5d9a0c900094","label":"\\u5e87\\u62a4\\u6240","level_id":2899790755,"netversion_u16":[41436,49307,23536,0,0,25536],"placement_sha256":"d9c0fe2a6818798e9bd4d03b11e9e5ad4acc0b9c94995b252aad3db1d0b40af2","resource_revision":"apk-v75506-421589","scene":"Night_Shelter","scene_provenance_sha256":"59c21f49251e5decc948d4ee35223c441e035d1a527ec4e6ef72072fea882270","scene_sha256":"685aa745c8ebc9aa8b22300abaeb60599002e237b68d8909b34193bee9a2cf05"},"2920756220":{"actor_sha256":"6951844433daf3d89a90405bc66757d3bd497c362b3192ff3a7d58f2923cebf1","cache_sha256":"0f184e6518be456a9fa466bf320632043b81ffc671b2ce8503e4fc818bc9106b","label":"Event_Personality","level_id":2920756220,"netversion_u16":[33380,49307,23536,0,0,25536],"placement_sha256":"bb07c36bf8d645e6ec2e32751cb6c6163c13a59dc37fca7b1849c84fd52f74d2","resource_revision":"apk-v75506-421589","scene":"Event_Personality","scene_provenance_sha256":"b9f52209988720259a8929149a620cd774143f498d8599873c9c0f0bc353bf85","scene_sha256":"02e86c3d07d338cada7b4488f9c4f249b0293b688029c6bb88db1ade924d177a"},"295816905":{"actor_sha256":"c7c414d4d7e9ea5b90348e7749e119218072980010c3a8b9dbc89871ac961765","cache_sha256":"8bbc4a437621948385e2e883085e3fab119fa00dc5a093cd5939a9d05879d3dd","label":"\\u79d8\\u5bc6\\u57fa\\u5730","level_id":295816905,"netversion_u16":[42307,49307,23536,0,0,25536],"placement_sha256":"8153535115d43c7758aacbb515a677764b2ca5d6828cd608b7895f123f153592","resource_revision":"apk-v75506-421589","scene":"TGCOffice","scene_provenance_sha256":"b1cdb4aba1ccba9dad68d973b41ce40a9a829fa1cc0161e9622b21d56c8303a0","scene_sha256":"853242c6faa7a866642a7d13566b18f556615d90dc474d4ded1c790e9131cc27"},"3051518728":{"actor_sha256":"86a4d28567da96f6d1b220e7d4db40662920250525104abaef9fa8ae6d6cad2e","cache_sha256":"f561d2ef04b83ebc3ae8d11b46d4a33b4794fbe018e50bfdf699a4de273c64af","label":"\\u661f\\u5149\\u6c99\\u6f20\\uff08\\u6d77\\u6ee9\\u573a\\u666f\\uff09","level_id":3051518728,"netversion_u16":[35732,49307,23536,0,0,25536],"placement_sha256":"46af93cc0f46c87b61afbc1d71b73b5745d6e0b4f0bd096a54920860e37f716c","resource_revision":"apk-v75506-421589","scene":"NightDesert_Beach","scene_provenance_sha256":"2aad520fd5b29842a0d79dcabd363eb73beaf21cd4cd1b233a67bde80ebb638f","scene_sha256":"b6fb0ec69ed3422771b1988c868d621ebd205f985fb33e325114ac3e94d775ee"},"3057325709":{"actor_sha256":"eec3120667ac2cd3f946a76e410e8e4bc4ba1435a842fe5986d43444889f2ba7","cache_sha256":"81f34bf36e0c42751c26177c9eaddfc5a0f92a5c2aae1a8bd3ae25938f81c113","label":"\\u5723\\u5c9b","level_id":3057325709,"netversion_u16":[13967,49307,23536,0,0,25536],"placement_sha256":"763bf8dc8cf87c12c814565f0e35e9cebf0a8064f9f5638cc57cf1687fa0b15b","resource_revision":"apk-v75506-421589","scene":"Prairie_Island","scene_provenance_sha256":"e5c2670bd8fa9c9aaafa184a419201c8387475894936451e9efc23e6c43125cd","scene_sha256":"7b9a766fa5d0a54720b4a2b9e7610050a3227f55274a88c6b635303b1e3dd27c"},"3082753918":{"actor_sha256":"ea474e4683d84fb30bde97e0318495070411c5c72a300259ff8bc4a292ea71fb","cache_sha256":"804688f938325de222a4bb99dbfb6c0fe001a9b031e1d9e8f682959a5f4ac3b8","label":"\\u8783\\u87f9\\u7684\\u6076\\u641e\\u6d3e\\u5bf9","level_id":3082753918,"netversion_u16":[21041,49307,23536,0,0,25536],"placement_sha256":"3bdb8ce5ede95fda44c08a632956d59f892c4582693f089732aee1c0bc3e989e","resource_revision":"apk-v75506-421589","scene":"Event_DaysOfMischief","scene_provenance_sha256":"d1cf1cca718c718a4d24a2d4d4266b12b0d9c791a03b58a6fca3d49400894ac4","scene_sha256":"79ac46e0eb934de0d6d6f70d3f730c4b3107f678567ed66abc9688feeba9db9c"},"3092325157":{"actor_sha256":"204d208c5793dede7fc715c7c8a99d2bfff3bcba72b271fb9672c1c6b7926273","cache_sha256":"baa6a075fd7ea4bb12bf851430d6a5272911e2cf74f2ddc4a1f9d8c62307d61d","label":"Night_GO_Town","level_id":3092325157,"netversion_u16":[38803,49307,23536,0,0,25536],"placement_sha256":"fdfd058dbd117cba356e20c534cc491cb9219db0fcabac57350ba1d83b00327e","resource_revision":"apk-v75506-421589","scene":"Night_GO_Town","scene_provenance_sha256":"58d4d471373a9311a7d6f1162e63c3e3a8deec17a7c8d5fda8e6d2fdfa9d0569","scene_sha256":"9d10ddfac75c893b1c547ea3544d8ca2a771c7dee27f961b9b7450f573ad9254"},"3110721718":{"actor_sha256":"ceb0f622f71bcbfe81a00a63a45485640b768b822ef89db2f6a5098181bf4a35","cache_sha256":"8ec195a1366f8a27d8ec3f484d86afa7ecc45481ce2ddfe7e7899ebe75087af9","label":"StormStart","level_id":3110721718,"netversion_u16":[36413,49307,23536,0,0,25536],"placement_sha256":"4c44ef85e61d0539cfc313964907c725f5a965a870a09954539a49b1000edd82","resource_revision":"apk-v75506-421589","scene":"StormStart","scene_provenance_sha256":"818fd5e20db9fdc32068c2bc011905cc5ebb985a3d762baaf3b24feb4bded0c3","scene_sha256":"99da084cfbe613a5a36c1bdab050d17ec283808391e94e9f8116917b26e8be6d"},"312004957":{"actor_sha256":"b7b5c8e2c6047a778e23840e230c8d6f9d8c5ce120642d1d962bb6889bb394b9","cache_sha256":"6155510e904986986b96b5e5355bd9b7e928f94c55ec4106a6cab0015970fd23","label":"\\u4e91\\u9876\\u6d6e\\u77f3","level_id":312004957,"netversion_u16":[57895,49307,23536,0,0,25536],"placement_sha256":"4dc74d731c59bd6749db8aef648891cdb9356dab16d483afae3614d58a03f5b9","resource_revision":"apk-v75506-421589","scene":"Prairie_NestAndKeeper","scene_provenance_sha256":"5f27e2e8f63e95d95e4aa97673b65f58db7216f0ae05ba274cdca29a06d56433","scene_sha256":"e098d2f5e96780fe8e278989f4373106c77868df7448fe9fffbcc5836f964a5c"},"3203226771":{"actor_sha256":"94c3ecaf67985277035276691888c3977e1f50c3c5ff836c45ed0306e3d3daed","cache_sha256":"9aed57d0373c97b156defb4a6d19e9cb64717cdebba54edc31f76513bf19c7de","label":"\\u7ec7\\u5149\\u9601","level_id":3203226771,"netversion_u16":[34241,49307,23536,0,0,25536],"placement_sha256":"213c90a39bb0ee70c98fec1356969945189bf3d3185ea53716301c399351a53d","resource_revision":"apk-v75506-421589","scene":"Night_Workshop","scene_provenance_sha256":"dbff6ff2691a02676ffeaf02351d6857a26e32500a29f560e5272f9c76a3a824","scene_sha256":"3b09d8df361b6e7cbf5df94991c1f6592a852414864a1fa5f9625dc3319fcd19"},"3244931597":{"actor_sha256":"29affd9b7e48d33b8132270bddb53fd28c1bee2153da42ac1ba2ea364691b9b4","cache_sha256":"3fb097b0f5999a0cb4163bb12d868b00e0e75845d7e67b5d59e0b568c4dcad58","label":"\\u5e7d\\u5149\\u5c71\\u6d1e","level_id":3244931597,"netversion_u16":[4656,49307,23536,0,0,25536],"placement_sha256":"1f558dd4cbc795637bed3540defb68313ce6ad7402b600269953b8c25240f1ad","resource_revision":"apk-v75506-421589","scene":"Prairie_Cave","scene_provenance_sha256":"cb290870609340f0027e3f663aa74f6b737a67342398873d4b0cc0385fa4912f","scene_sha256":"0ef81f603cf9eec2da59da66da2d418031d0bf4484cb3fe12652178bb3ba990b"},"3258961934":{"actor_sha256":"4fbaff42b0cc0c9076bd94c87bce4016c47e5881853b0f40adc80fd8861f4873","cache_sha256":"e67558ae13b5c7bcb3b29fbd6ed735d90e821e87b2141cc1a721c9559a7ff846","label":"Night_JarCave","level_id":3258961934,"netversion_u16":[2938,49307,23536,0,0,25536],"placement_sha256":"1c396a1cc86cd1cb09688b7d65c17067fe9a0e58c76be0e5a73a5041397ab9bd","resource_revision":"apk-v75506-421589","scene":"Night_JarCave","scene_provenance_sha256":"1b76440c252bd2e369a142d51655308150fd17ce0b29941696d34f8894f6074f","scene_sha256":"866a3bb442b0ec310a99e1eb85e371633914acc59aec2186435f589902537583"},"3317260872":{"actor_sha256":"4c21970b39b03cc8711e139b1cbb96cb688ba275881a2ee28e164be0ae90455a","cache_sha256":"8b51bb12e2844d92a7c3c6ec7bd175ad57252275f9bed0d325b23b0aaaab2eac","label":"\\u5927\\u6811\\u5c4b","level_id":3317260872,"netversion_u16":[8826,49307,23536,0,0,25536],"placement_sha256":"ad6dac8218fb4fa0d36c17c27fd6d1ba352d9f4457a87b7a7dfcd601db9d6ffe","resource_revision":"apk-v75506-421589","scene":"Rain_BaseCamp","scene_provenance_sha256":"3498b9373203d52247db9e0d712f30a86aa48ee2ff60804fea743a9d0e6c4acb","scene_sha256":"82722e0795cbe7b144095954e39409b136ceae142e861ca05cf7e4b3c61795b5"},"3368878998":{"actor_sha256":"02f568eebb48be293120c1cfd2c1bda9c62241463dfe0ac99a083ad59659e6d4","cache_sha256":"cf31f1b5fc488220ca95e1a07cad9e4e0d90e8c3100fbbabc9b3d4ccff81d18d","label":"MainStreet_Soundbath","level_id":3368878998,"netversion_u16":[34484,49307,23536,0,0,25536],"placement_sha256":"e8743ae5ab002545bc93c39860c7d167534fa90ec4040fe4d0f567c4fc2855ad","resource_revision":"apk-v75506-421589","scene":"MainStreet_Soundbath","scene_provenance_sha256":"7f44ff53313920635791e205ffa2326e4af409e465f198a07361fcc9efaddbbb","scene_sha256":"96029dbb28ca9fbaa270ef58fbfae38a2320797d1fda5f28481abfecb086962c"},"3429122502":{"actor_sha256":"6f5c0b127502b276625f2ba2dd79a9ffc15bb9f2fdb6530cafdd2f73dfcec923","cache_sha256":"062c6b80c7a7444ee0b75a96cfc716e62547f7e4ab438bf0dcb183602f33e0e6","label":"Prairie_WildLifePark","level_id":3429122502,"netversion_u16":[49588,49307,23536,0,0,25536],"placement_sha256":"a9a4d0449c33145ad88afcb9b552d6ea177c625cf8ae9fd8770bcc5f754d1f8a","resource_revision":"apk-v75506-421589","scene":"Prairie_WildLifePark","scene_provenance_sha256":"ce45616847987063bdc06f42d4c26a658fdae89880b454acb972daf266c596ee","scene_sha256":"b3ad9c15de662eb1580669c089a411f09d38e634a584ae0670fa34d0bb3e5d33"},"3437135515":{"actor_sha256":"54a844bc3c549f3665533ef37b0822b6369fd0ddfbec91a60e965e164b9791dd","cache_sha256":"03deb6a697ddb2adac4b0d90ab36d1ba6998e15d46ec641a91c426a469593d93","label":"\\u91cd\\u751f\\u4e4b\\u8def\\uff08\\u4e2d\\u6bb5\\u573a\\u666f\\uff09","level_id":3437135515,"netversion_u16":[15789,49307,23536,0,0,25536],"placement_sha256":"11d283f762529505ab4ef0f671dcf4df17b410210eed4bcbcf46b90379cac05f","resource_revision":"apk-v75506-421589","scene":"OrbitMid","scene_provenance_sha256":"15b099300ce043121187f94200f3108a4d94f5d293954fb57a221fe42a0e3462","scene_sha256":"711b8d3578aa99f4f0e7b1409e250b097d8f32af9a6077b1052f8b404f335826"},"3452301396":{"actor_sha256":"75b8b7ee4697d81ce3348e7fb8b18b497b109bea8143b0bfa5a516860c36349a","cache_sha256":"9fa87fb1985e1ce469aa5c4d53148cb0146ffb35e2a6c21e58efd0926497fbf6","label":"\\u6708\\u5706\\u4e4b\\u5730","level_id":3452301396,"netversion_u16":[64024,49307,23536,0,0,25536],"placement_sha256":"4c2c5704648efa25cec18437563e402ea79ae4375650670aa2ef69bdfefac11e","resource_revision":"apk-v75506-421589","scene":"Event_DaysOfMoonlight_HugeMoon","scene_provenance_sha256":"e1460b5589d5a7a8422ec7aecf92f92edad50fcc8474300ed4f1385d2fdadacb","scene_sha256":"53d633f17fa614287a330cfbd65e595a1230f188b3793539edf5f6886d8d985f"},"3463762797":{"actor_sha256":"105027bc22f6d9f976595a9dd861a10d3ff35aa85153c91cb5f1d12d71550653","cache_sha256":"f06dfcec8286c4bc75e8e60ef7e607f394c8c45a4c6fd7266e6abf077a0b3261","label":"AR\\u5367\\u5ba4\\uff08\\u5185\\u90e8\\u573a\\u666f\\uff09","level_id":3463762797,"netversion_u16":[35344,49307,23536,0,0,25536],"placement_sha256":"04b46939aca078ca20c8e44debb4df4689829a6a447ecc70cf6b35130aad4bb0","resource_revision":"apk-v75506-421589","scene":"AR_Bedroom","scene_provenance_sha256":"b63d0d15bfdcd3f8700c3057b4a922bd13f5f14092e87559010d0a7ea202f05f","scene_sha256":"8d26d3aa587bc6608e1c35ad62143901e7a2b8857ef0de19ee8fdbd86b8149a5"},"3479786579":{"actor_sha256":"64746aa292ed96bd935bc679123a3fcf9cf5ca62c5b61811e1a0d65ddc4d3dc2","cache_sha256":"ec4bac529ed1afe672918a1ee88cf9f5bede6da5daddbc4c27d52221d13bd734","label":"StormEnd","level_id":3479786579,"netversion_u16":[10808,49307,23536,0,0,25536],"placement_sha256":"a34cb403c1fbb8d47c088accd8ed5d80fc67d41cff4ec09416af01c37fd7d915","resource_revision":"apk-v75506-421589","scene":"StormEnd","scene_provenance_sha256":"a564624b45ac665cb2a02f9c3a3871f5e225e6be1d1875f10a0b918dc8a29536","scene_sha256":"c8f915fad61cabc21d9feb258424f1ea139847a07dacf067f78b9791513bd1b2"},"3493360294":{"actor_sha256":"fff84917324b2788580b17eacd5a801ee421065390148807ddc0a60e51d3bc0b","cache_sha256":"5f9e4bcc5c200b07877b0037516dfc7c85872ad82d47ab43276fbe1af04b143a","label":"\\u57fa\\u5730\\u8ff7\\u5bab","level_id":3493360294,"netversion_u16":[10709,49307,23536,0,0,25536],"placement_sha256":"de6b38c106a80d88d0f9d8cbe2dd29083780de13fbce8108811b544ed0a1cffc","resource_revision":"apk-v75506-421589","scene":"UndergroundOffice","scene_provenance_sha256":"5d4079b1f8870c9674054e2cdf6b0ba10ff5338fcaf05bcafcf01244c36d3a6e","scene_sha256":"87386e39ee8e1e07a76e87af338c4b9cc1f7623b7b484abdaba0539e867a3000"},"3516048122":{"actor_sha256":"f1bae32c5aa7339832313d07dc72857355f922b384e55115c77a1c027822703e","cache_sha256":"d8eccfa873e9f548a33ca032f026b2374a577a5535ccb537811c419702055b87","label":"Dusk_TriangleEnd","level_id":3516048122,"netversion_u16":[386,49307,23536,0,0,25536],"placement_sha256":"29189f2c40370891e84b177271077c9c92bf158bbf6bc9f3719a119a8d032ad3","resource_revision":"apk-v75506-421589","scene":"Dusk_TriangleEnd","scene_provenance_sha256":"87fdbb08fc3c1855990f7d7e1803d6ab47687f6d0ec6eadc2feac2a39229a8b1","scene_sha256":"bd1343e39edaf601952754905f18001a403a952af070877fbfec95f83fe8c146"},"3526133726":{"actor_sha256":"0a5562753d1c0ceec073a67b229fc367ef0dc46b511689d1994b1479e646df1a","cache_sha256":"1d16b337c435dc252a2166d4ef60be9f5873e32df10d27015e132db21c45ff7c","label":"\\u9047\\u5883","level_id":3526133726,"netversion_u16":[36851,49307,23536,0,0,25536],"placement_sha256":"04e7436fb43d9d09f443c5574c7cea57bfaa27d40cc4021c49ecda6628866c2f","resource_revision":"apk-v75506-421589","scene":"CandleSpace","scene_provenance_sha256":"e73a4563aa4c9233b62880166edc8c36bf88984a1f191b30b3569102932239c6","scene_sha256":"076ed61b77159b8b33c1e0cc7562145b78620837ee5eb92c5f5a9e7be756df38"},"3530028462":{"actor_sha256":"d7390c30b52231624b196f28fea75dc2a152841800cb6c9a572b54e76a175564","cache_sha256":"8ffb313169e0ef233b5a21ba5e83e68ae45db4702c58917e95c0b4183acf9a17","label":"Event_Arr_Warrior","level_id":3530028462,"netversion_u16":[55898,49307,23536,0,0,25536],"placement_sha256":"dbd065a0c4d50ba75f0441463a86ea29760df2d13d54cc85d7c0b75277e64330","resource_revision":"apk-v75506-421589","scene":"Event_Arr_Warrior","scene_provenance_sha256":"a0f2b2c7f021079aa65fa8176ae754b972655ab2369a929c3c1e3b594f77086b","scene_sha256":"1cbd11f5c1c88ebceb23312f00e18033f9cc965a5ffadb136d159433d059d840"},"3530471012":{"actor_sha256":"d7fcbf50e0d63dafc37f81d4d8a0417b150fcdb3fb08ede578dbf78b97775dc6","cache_sha256":"fae8e93bbc4b8d8ce78ff7d0a4d0739238fd8e00142d6a882c108b6b58ba88d4","label":"Event_HidenSeek_SnowVillage","level_id":3530471012,"netversion_u16":[3722,49307,23536,0,0,25536],"placement_sha256":"78b0ad6b0e707fd6583d95ea54e2e460d635863cc77439308ee8644272d1752d","resource_revision":"apk-v75506-421589","scene":"Event_HidenSeek_SnowVillage","scene_provenance_sha256":"e977ea30a15ea434c5e83aca03f74a9b9d95e0a35bcea70c7dd96914184e83c9","scene_sha256":"43a88f1afac32b07cf15e3d980001407dee77f99a16e58b3239a4646338dd3dd"},"3667302771":{"actor_sha256":"c92e91dfbd7887b7fa4fbb0904a52291d66d6d1e570d935c98d09e23072f4744","cache_sha256":"9b435424e593aa3d6cf00d31acb3b1131dd7a761196e1acabfc26de5462c0b06","label":"\\u865a\\u7a7a","level_id":3667302771,"netversion_u16":[49811,49307,23536,0,0,25536],"placement_sha256":"be0db9cbd49a46f98f304255a3ff8f54ae14a2bc8692db41844c74ef3cf346f2","resource_revision":"apk-v75506-421589","scene":"Season24Void1","scene_provenance_sha256":"f59d0da0fe2011e32ec7270f79e9b697f016b53f9fd92a6bfaafdc683fcae9d9","scene_sha256":"6baaea3e11a20316eed5709b49ccdcc80dec95d51201d5f89689c00dea176e0e"},"371156238":{"actor_sha256":"e72a3bf0bc32ee29e634991fea909468591df69b522649ded6a9c1a52f3151a1","cache_sha256":"3d2c4eb253247c948acbddf80ddd4b5abf2812ebf9ad333fdd080d1c6b6ed6e5","label":"\\u66ae\\u571f\\u795e\\u6bbf\\uff08\\u8fc7\\u53bb\\u573a\\u666f\\uff09","level_id":371156238,"netversion_u16":[591,49307,23536,0,0,25536],"placement_sha256":"41201d1e37b306b2ad2af718a265e2ebb00efcdd8b2c1d734b8ccf92ded3c252","resource_revision":"apk-v75506-421589","scene":"DuskEnd_Past","scene_provenance_sha256":"e483d004ef3cf0ff05e42aee5d2e7fc5d4be0506b9c438ea0beeaeebdfcc3316","scene_sha256":"13f383793da4396427d83c8aa23238ae0709d141eee6890eb924bfc93f65c81e"},"372393162":{"actor_sha256":"814cfd096709a689237907bfb1e0732ac41ef377976adadc467afcb5217ee118","cache_sha256":"80620318dc44af824212a64ca931f961dd69b31555f9eb770880b55c9213ce02","label":"\\u7b51\\u5de2\\u5de5\\u574a","level_id":372393162,"netversion_u16":[31779,49307,23536,0,0,25536],"placement_sha256":"a0820d5356981a4eb49db9839609c74694ebdebd45ed77b80bd360bedb9da4de","resource_revision":"apk-v75506-421589","scene":"MainStreet_ShopProps","scene_provenance_sha256":"d42b242e3b0857b125cfac4a6fc6959e053bdd46cd1d7a3fe63b371ca2f776e1","scene_sha256":"e9759d17046805da583953e30aebcab66b217c42927e4d6b46ae732366b26905"},"3730271824":{"actor_sha256":"1f1e28cc825f2ada52e52a4837ffa0d329442f891675d75bc8022f3994c3f78c","cache_sha256":"1343b91e0c171ae37d28e75369cf2e8b11baff1a0b3b6ce30a8203ae0b5e7489","label":"Rain_BlueBirdTheater","level_id":3730271824,"netversion_u16":[667,49307,23536,0,0,25536],"placement_sha256":"057b9182f61de2b6c7e175c15389ca6ab2085e46d669190486060bfc91932608","resource_revision":"apk-v75506-421589","scene":"Rain_BlueBirdTheater","scene_provenance_sha256":"8ddf55440592ba3515ee594ccf3c2ef4f6706012454c1c0b8a9e8cd4dacd64b0","scene_sha256":"c40f1f81e9d9f353860a4a255244a90b729b68651c16bd40810077a13c4c2dbe"},"3743646255":{"actor_sha256":"49867f593448c4efb5f658e4e186e5b9e13301c7d6391e6b414c88badc004a6c","cache_sha256":"9c0a3306f2a57c8e7cb1766ba9433e1d251925378a14f6bb96eddf8f81235549","label":"MainStreet_FunArmada","level_id":3743646255,"netversion_u16":[59928,49307,23536,0,0,25536],"placement_sha256":"ad2569612d4244c29a2f6d1c5bc12a4e1cecb4f397f6505389c81e6528ba4835","resource_revision":"apk-v75506-421589","scene":"MainStreet_FunArmada","scene_provenance_sha256":"893df57d9d51625d1c5158f2bd3b3db71670df0e32d3d60ffebc605b929ea00e","scene_sha256":"12aa9e3514968622ba6b082a6a4ccb60d63a4bad3fe1d7cb70e3f2803ac1331a"},"3803142938":{"actor_sha256":"042b45bbf4d05b426a7c5124286f0da499be507ebb3915493fdb2b0b37ebc48e","cache_sha256":"a972b242c97d54f64bfcc0bb214f363b03b1913bff949a75941c87ab9aedd6e3","label":"\\u59c6\\u660e\\u6545\\u4e8b\\u4e66","level_id":3803142938,"netversion_u16":[50914,49307,23536,0,0,25536],"placement_sha256":"903ece170dfd081d16220467fd970c209e3331c64f32379a19d60edf79ef597e","resource_revision":"apk-v75506-421589","scene":"Night_StoryBook","scene_provenance_sha256":"8f943d4ad095a5ccdbd0e675236deed0e37d33a16d0d54f06d7c6f9a91500451","scene_sha256":"6cbdc79ff30c4a91d40eba76f8bbfe706834446fb89cbf01d52b07696ce36597"},"3884142720":{"actor_sha256":"e2544c489ebddd2336012d086660cf776e3f40c63c8d97657a136f5a4c719f30","cache_sha256":"94a1973c130cfa1042a234b9e5ea44ea1402b189e9747c3c1a5cff9f45c4c2fc","label":"Dawn_TrialsWater","level_id":3884142720,"netversion_u16":[50622,49307,23536,0,0,25536],"placement_sha256":"ddc1de309e1f2e98b4522bc34d8c12f153998f6a6ca4b0d39eaf8580ec604fb1","resource_revision":"apk-v75506-421589","scene":"Dawn_TrialsWater","scene_provenance_sha256":"5823946fb93e2af8fd5fae7610904d9e27bdd6be147239945f57f533789bf59d","scene_sha256":"c3528a294abfeccd16d1e5c693dab83d983a1e458a4e608c89febb29dc536159"},"3905084854":{"actor_sha256":"73a93b07388549fef47580c9d47589d015c3ea29a4898e30638981c6f3fc5dbb","cache_sha256":"ec353507c941fe35cbbddb5c6b03aa7bef13652c7118ed513bf4ecf041cefbda","label":"MainStreet_Apartment","level_id":3905084854,"netversion_u16":[15232,49307,23536,0,0,25536],"placement_sha256":"997734b80a2f114af3eb40eacb9a0dc26952a873e13e763eef2cac983b85d442","resource_revision":"apk-v75506-421589","scene":"MainStreet_Apartment","scene_provenance_sha256":"913f7a65300e7facb08d57c9611e0ab4fe9e74444fd30b7f6928b99109942a76","scene_sha256":"4c56f65b36741b0526b17af9b0d5aa9998fb59f8e17fe6a4767e0fea08f83b05"},"3928396665":{"actor_sha256":"96eb7a7a73d4baf0d8f2c31b8d550c601f3fa9e268def621eeb273d727b849a4","cache_sha256":"825dffb69d5445eebfb0e7bf6f3b72aca7f78f5a392196a31d5471f888936b99","label":"\\u8fdc\\u53e4\\u56de\\u5fc6","level_id":3928396665,"netversion_u16":[64389,49307,23536,0,0,25536],"placement_sha256":"db20d39524a73b1062f001a0d1326136ab122e02ad32d9cd2f1ecfe051183c7c","resource_revision":"apk-v75506-421589","scene":"StormEvent_VoidSpace","scene_provenance_sha256":"0e58a6351639edefd2363646d2d92148dbc25dcb3ed9a37b40d185e99c8fef34","scene_sha256":"0e9d9750496091c89c12918a8cf566c1cda7df160e8ec74b5e27f362ef87b991"},"4011385311":{"actor_sha256":"69bfeb616ff928df0a2e45508eae2262724d7f941abc152ce95772246e3914ad","cache_sha256":"9e6e97ca0c4d59689303949deb66430784a3150c45c04044c7fa60481048034a","label":"Event_Arr_Runaway","level_id":4011385311,"netversion_u16":[58451,49307,23536,0,0,25536],"placement_sha256":"cf2efafbb5fe5493ecdd8aa2eee731864010102a287eefd949ce9a0d19434dff","resource_revision":"apk-v75506-421589","scene":"Event_Arr_Runaway","scene_provenance_sha256":"e2bcb40ccc8cb01d568781515aba099f307b136810e65720f2412fdeefc22015","scene_sha256":"3fb44617a858afae1127983c83c536f4fcc9715ec823aaab7dbc8cdc9c79b456"},"4023907509":{"actor_sha256":"3ddb919af9c734a367652788de7720ea20e7ceba5a36a445a0ea73dc66a46e40","cache_sha256":"f12d1032bc0a498e01b49876a92f79a439c4c492cf883816054398eb9b41ae68","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\uff08\\u6811\\u573a\\u666f\\uff09","level_id":4023907509,"netversion_u16":[4466,49307,23536,0,0,25536],"placement_sha256":"b3a0091f5670deb42f80377d830be4ae70de3b90a93bee6b6e70e8cdaa0ed8d6","resource_revision":"apk-v75506-421589","scene":"Night_GO_Tree","scene_provenance_sha256":"77d80687b776e0637e91610fab977c4d1ab3aaa7750931d2edeefd0a6290c2fd","scene_sha256":"2492d0444916bc0998c97d2567ad62724a1c746f1c6d746ba33bf8eb4bcba015"},"4044469303":{"actor_sha256":"3a0bb38ece9da0aa19dfffa1745802aa53a9ab411312e64b80be6063902cd06f","cache_sha256":"de3e74b9116153e5771f098da5da90dcbbe4d5199e5ab36d45dea974c9149673","label":"\\u9b54\\u6cd5\\u5546\\u5e97","level_id":4044469303,"netversion_u16":[32106,49307,23536,0,0,25536],"placement_sha256":"35e7468585e7ee84c3a7a62a82781276db55c43da1ffae781e4a8644fa8480fd","resource_revision":"apk-v75506-421589","scene":"MainStreet_ShopSpells","scene_provenance_sha256":"25461c58ef70abc5d866c49eddecc65b07e58d464478040173912cbbbe61a3dd","scene_sha256":"d3bcd110137a13c6fc0248e9bc613488ed66956c3dd033e08f802a51c5c5ee85"},"4070111637":{"actor_sha256":"aa5f0e224bc99fc918e6c7c3e042858720484213f157ca6769e697f70e433538","cache_sha256":"5ad81f948971fa031bb4c50657b327b5c8b11af3eca5e1e12370fcc94acdf93b","label":"\\u85cf\\u5b9d\\u5c9b\\u7901\\uff08\\u4e3b\\u573a\\u666f\\uff09","level_id":4070111637,"netversion_u16":[2032,49307,23536,0,0,25536],"placement_sha256":"6ecf3dd2c49711b13b2b4ec34adcee84755e416b7c94b712c15a2ac549b33909","resource_revision":"apk-v75506-421589","scene":"Dusk_Triangle","scene_provenance_sha256":"fd477446d56e207c9b2c0d995f394573de2d00c078dba629ffa2d2f5e1876f2b","scene_sha256":"023c7c49fb5332b8df18b3a2da372642ef847f91a95624956242ce3613a7984b"},"4133595729":{"actor_sha256":"b88ce51dd767e5cf822259d5dbe8485e76a1ca588b9cdd153d09c40408666d7b","cache_sha256":"ddf8126f536bd1261415a8da9db1ce0e117c6b17fc76e1eeb643c03d31766e8c","label":"\\u5730\\u4e0b\\u6eb6\\u6d1e","level_id":4133595729,"netversion_u16":[35163,49307,23536,0,0,25536],"placement_sha256":"d478082d03385a0c125b39cbab3e5452316c126348c497fe7f67621c1a0e0fde","resource_revision":"apk-v75506-421589","scene":"Rain_Cave","scene_provenance_sha256":"3421f8b96b2437f8e7038d96f3cb65251108e57fb7a7b3f7ac9aeffc3f0c9800","scene_sha256":"83ec9a77f4a233328ce9924db2560282595545187f6ab41b7d93685043316a8c"},"4158956653":{"actor_sha256":"8f25e35eabc04e03d19b5c453e7466ba4b18564625707fe1a7275debcd06817b","cache_sha256":"98c2edf344e7aa13376c223607382bd54a5d703dcc6ded8a6344d71da1a541c4","label":"DuskEnd","level_id":4158956653,"netversion_u16":[54113,49307,23536,0,0,25536],"placement_sha256":"fd15ef43c1e00b9cc3922fb0bb2ab8698549a32a454ce4f4c468bb22d790761c","resource_revision":"apk-v75506-421589","scene":"DuskEnd","scene_provenance_sha256":"b553572302eb9da87c47ea9d3af28fd3587ae589b92850c8d01a94ac7ca2529c","scene_sha256":"a0d86981ef2605a101e201b36668565c01dea2c2b30dcdd012bbb3f8dd8f09ac"},"417578099":{"actor_sha256":"abdbdedb8f537d31c3867d0410bc9ea0227fa0d95216bec408b309f7d8dce025","cache_sha256":"171b687178f7b9c5b17d5803ebbf0e810d3395cfcf1df271e5fe74934d0b267a","label":"\\u7fa4\\u661f\\u5f71\\u9662","level_id":417578099,"netversion_u16":[14292,49307,23536,0,0,25536],"placement_sha256":"6f350890d11c773a9ea427abcfc0b8f5c268c34dc2832f2b7cf8da8f6ab16af0","resource_revision":"apk-v75506-421589","scene":"Event_Cinema","scene_provenance_sha256":"ef878af8d89ebbdb9bdaa363a27dd0300103a4019aae0725af77e11b63236411","scene_sha256":"b52069ad48c8acdcce769a0a3bd275ca1ad4ba5f2c38b541d21acfd02bbaf06d"},"4182564783":{"actor_sha256":"000b5dd97519ee462f85e05a5fa6710944f84fe6c15053374da864e7d95e8151","cache_sha256":"6b7f87d43e4e0083930e092f47dc738c60db3ecd15cdc818261881f7de88fe2f","label":"Night_Jumble","level_id":4182564783,"netversion_u16":[19522,49307,23536,0,0,25536],"placement_sha256":"09722050da2741b34724e045518e072dbe892b986bcea8e37c3c97a036883c8a","resource_revision":"apk-v75506-421589","scene":"Night_Jumble","scene_provenance_sha256":"5a35a0e27e6e437b76c3d0a8c7148145dfb89f51f6b6a074aa55d33f217fde7e","scene_sha256":"053e19c97ba1eb99e7015f5cb5b713bb74973f0a78ad16679c90e51f3decbd70"},"47024056":{"actor_sha256":"8ccbe4c1aa68e03870f2f2781db049c010d9210e4852e1756e15a22164480f0d","cache_sha256":"ff2d8b9af29278a6f254d353f915cef3ce1f7361f098c77c90e73c096596d1de","label":"Event_Arr_EyesOfAChild","level_id":47024056,"netversion_u16":[47197,49307,23536,0,0,25536],"placement_sha256":"fa226d765920e8bd87b0c0a353e56ddad1ff2c0d8526c43ecbe60864ebe8f5f7","resource_revision":"apk-v75506-421589","scene":"Event_Arr_EyesOfAChild","scene_provenance_sha256":"79478cebf23a25492bd8bcda25e1df960fa983926f3b24ae8b7fff618b2e4871","scene_sha256":"9515be9f47b4a6d61ba747db9af1e37735af70c370a5d4533825aa92bd8cff7c"},"507487826":{"actor_sha256":"17d02195417279556d034d571ed847fcfd57e4d127600857524f81edb52c49c0","cache_sha256":"5c529f9aed30124564aaee5e034b2351762c754dbbf19082309243faf0871d97","label":"\\u971e\\u8c37\\u795e\\u6bbf","level_id":507487826,"netversion_u16":[17841,49307,23536,0,0,25536],"placement_sha256":"d244e7cbcffcad4c1677982f2939d9660d524e42d297595015400be0d2b462e4","resource_revision":"apk-v75506-421589","scene":"SunsetEnd2","scene_provenance_sha256":"decb7bbcf0be234df12a494b1fa302ac2613dadaf2b4720ff3030f87c6a07bb4","scene_sha256":"515e588460a90c80ff7d805bef9a7543370ac99ef66a35457eadc7fb0785d58f"},"530831471":{"actor_sha256":"f805d4982d94d86af3a6c34c5be86d3f46c36265f98827ca7655692ada490d71","cache_sha256":"c26e24eaae3fbf63125e8e61989b29540fc00bfa8f063e4d883542d08bf91f99","label":"\\u85cf\\u661f\\u9601","level_id":530831471,"netversion_u16":[29786,49307,23536,0,0,25536],"placement_sha256":"315b0ced5f5324b13b010602f20ae596cb56d5f02804eafc5892e189e7a32132","resource_revision":"apk-v75506-421589","scene":"Night_IPHallway","scene_provenance_sha256":"f3cd69c38bf6b834d15ce6623aeb140fd65d0e48a33217ed191d590f6f4682b1","scene_sha256":"a3d83ef04a7c4d20b2501fa046530ff8e84790a9dc51cabf81d78c2f8a21336d"},"567986524":{"actor_sha256":"89dfbe640d00dc101fea260bcce57add869d0ab79358bee70a00c352a597d031","cache_sha256":"857ba3e351430f312a7a7b8d79ba24be0a32cd6933152e35eda8396e50eeff24","label":"\\u91cd\\u751f\\u4e4b\\u8def\\uff08\\u7ec8\\u6bb5\\u573a\\u666f\\uff09","level_id":567986524,"netversion_u16":[28077,49307,23536,0,0,25536],"placement_sha256":"c46df2314f61e1022aa95c51e45764f8790def775908ad0e74d5f7b920d85e13","resource_revision":"apk-v75506-421589","scene":"OrbitEnd","scene_provenance_sha256":"3fd7e7cdb75dd2b376a5a3eba742ac36ad5ea1691e97f8b1e4a85d24f48ffbe8","scene_sha256":"d55366e2475f77bbdcd8058b7c0c5cb68c0d7b38dff70f2ea983cc30572cdf0d"},"571720490":{"actor_sha256":"c77d6997e0831e6a0f18f294afa70b1854d70179edc3ec98d9343b859e2b8c8c","cache_sha256":"de6ae004536b5b3bb8cb8ef8d1f3dd7cf7b38833f1ee4481480af3e6e382ef58","label":"\\u6ed1\\u96ea\\u8d5b\\u9053","level_id":571720490,"netversion_u16":[41011,49307,23536,0,0,25536],"placement_sha256":"775a5be0993533509b2cd0335d75abd8f1a6b26924c14466163359db8249a9ea","resource_revision":"apk-v75506-421589","scene":"SunsetRace","scene_provenance_sha256":"fe595a56b8481857a92d829255cfe73a1eb9062b78893f5c93b1db59e0e341ac","scene_sha256":"d865354608200a0962c3c0539dea74f224bf2f359aaae6873113a653e9680d1b"},"607516458":{"actor_sha256":"bec1f74076d15c0bcfb5bc05af675f7cc58ab00ada49a7ec3f91d4df38c11648","cache_sha256":"d595a457ea9f3e72503dfa7a2f674df60b14b8b0882a24c1fef415700da00a81","label":"Dusk_PearlIsland","level_id":607516458,"netversion_u16":[55327,49307,23536,0,0,25536],"placement_sha256":"8808c431a18109d1fb19bfb926907ff84ffa7046b246570f2cffa52502bb82f4","resource_revision":"apk-v75506-421589","scene":"Dusk_PearlIsland","scene_provenance_sha256":"70a98d4f08c61222fd34cd0987c85a976c3a43281d7b57169fa1f1634cf12cfa","scene_sha256":"b91732fde2d4faf2ce214c379ee315fd136ac1d1e90c1d0a9c78b48060a1ed54"},"649101397":{"actor_sha256":"64c8d3ccc6c18b4c4c98f9d7e5655b1d6b16b5335f0212ddd16802e21b8fb483","cache_sha256":"7a0f6a672a9cad8730cbf05de832e12e131e0704ea81010639a066c6479515b3","label":"\\u96ea\\u9690\\u5cf0","level_id":649101397,"netversion_u16":[2078,49307,23536,0,0,25536],"placement_sha256":"ae54d3f232deee88c5a9c52e7f330c29069190d4c17e91663644f344ef06f9b8","resource_revision":"apk-v75506-421589","scene":"Sunset_YetiPark","scene_provenance_sha256":"012b42814571256d5c09b1bfaf98681661380f0f9285ab43a706b960c0d9e9df","scene_sha256":"b70312a91a8820ddd397dc46e2ae364b792d4d0fcdffdb00660a2090708fdad7"},"748712866":{"actor_sha256":"4d3870e5f8757cd39270169477510a79b2b27fd8628dcca2019f02af9d790e0f","cache_sha256":"003f3a26cf407bc726bd9aeceb5fcbb34e12c836848856770732b406a06bb432","label":"\\u9884\\u8a00\\u5c71\\u8c37","level_id":748712866,"netversion_u16":[38306,49307,23536,0,0,25536],"placement_sha256":"3daadf74e3a02cda54a5fc08c8e6ce1e1a3d65ef37b83a8c2360d28eaf740d2c","resource_revision":"apk-v75506-421589","scene":"DawnCave","scene_provenance_sha256":"15fb230ae2c8fa31f7d4f7f5f4b8e5d6d890cea286cd24029dade2200adfd5b9","scene_sha256":"ae17f7f1317e6d53174c345f0ce5a533ad4bfc6aaf3e633591126ebe36abd387"},"817373972":{"actor_sha256":"e644c9edce17de98d72ebb1b5516608dff8463fddb1723bad1c3851009b8d445","cache_sha256":"876422d0c008221f3c5c51b750b05d406c00534c71679c305a7072a7e18b27c4","label":"\\u66ae\\u571f\\u5927\\u5385","level_id":817373972,"netversion_u16":[64828,49307,23536,0,0,25536],"placement_sha256":"b63dc86c8b9b3c43025a3f830704654d26216f4e4bc5e837ff02585fde524d22","resource_revision":"apk-v75506-421589","scene":"DuskStart","scene_provenance_sha256":"d8c7ce8902a7e600ae7a26e523d4a682f1243aed3056ade3b590b1a490ff488a","scene_sha256":"6d41057746d5f3abe7398aec67fd3dbad048694da8b1f0e86130c53fde3a77c6"},"864432821":{"actor_sha256":"d49dee68d0d8e7d19a1da242649fdc67d48dbec44fe0eaa0f47b0a87711aad0e","cache_sha256":"c1920bef28bca9b2ff5485518b4d5d21821b9819f039ca97893be28fc3b6b0cf","label":"\\u5de8\\u517d\\u8352\\u539f","level_id":864432821,"netversion_u16":[19939,49307,23536,0,0,25536],"placement_sha256":"b239b81e261ac9423489caf72e14354561764cc10bd2dceea865ac208aa7d8fb","resource_revision":"apk-v75506-421589","scene":"DuskGraveyard","scene_provenance_sha256":"0797e837373ce2ff2dc19e63d2cdf8d18579523a25bd6514b90d729f6d13b35f","scene_sha256":"54e0399e364045bd392eb243ff07e2ae5d7b61e339574027b3e2566cce4352d4"},"961583455":{"actor_sha256":"7d85220317f7bacc5237e0815b257b16d265208bf67eb271b97ee4078f7b4c58","cache_sha256":"6dbfc303533525507a72aa159d20c18dc9eca037fb828a3aeb68597dc9ac5104","label":"\\u5e02\\u96c6\\uff08\\u72ec\\u7acb\\u573a\\u666f\\uff09","level_id":961583455,"netversion_u16":[49107,49307,23536,0,0,25536],"placement_sha256":"347b3d8209e490015903c6a958bfee90f040c55c9278727ffa363a39cf749184","resource_revision":"apk-v75506-421589","scene":"DuskMid_PastMarket","scene_provenance_sha256":"c1020236ebb712409d8be2dbdbad7d1d89fe518bf64fd0d6630072709bd8f08d","scene_sha256":"81e9198f8e6dcd59051da03c440d6fd10bd90583e86854ff04549a4cd08a1f38"},"98108829":{"actor_sha256":"86073f8ae030b2bf4522a92fef6ecf1743d87f566e63b483662b6a96c098f73d","cache_sha256":"4eaaadfba34b3d970ec6a8b3b0d3bb36c5e1a884fe52603504c5e6ba2c64b171","label":"\\u661f\\u591c\\u753b\\u5eca","level_id":98108829,"netversion_u16":[46138,49307,23536,0,0,25536],"placement_sha256":"e116c7774b0026be18e7d5fcf1008500463a9e7383b5a6c71e7611ffb905262d","resource_revision":"apk-v75506-421589","scene":"Night_GO_Gallery","scene_provenance_sha256":"5512542f2554c84ae6e2272ceb0736962ab45231c4e5e06760c55db3f01e3b6f","scene_sha256":"abc4fa78759aa1ff31769b8f9e31df00e8c6accfbfc63cf89c7dc4ce0be422af"},"996052172":{"actor_sha256":"9de045e022e8012fe5d0ce4d5762cc34db3587b8a5de93e8282d714502ff5d95","cache_sha256":"a7dad6b027c69e4cccd4bdf49e1565ee600b69a09ae2c39a951130a2241fee6b","label":"\\u81f4\\u68b5\\u9ad8\\u5b63\\uff08\\u5c9b\\u5c7f\\u573a\\u666f\\uff09","level_id":996052172,"netversion_u16":[23855,49307,23536,0,0,25536],"placement_sha256":"6ffa936861f997ecddaa671bc9f9dde4a7db5e8f64bb7a78d2336db96b22d0f9","resource_revision":"apk-v75506-421589","scene":"Night_GO_Island","scene_provenance_sha256":"94930d50a8af053896f9f5913848720aec146b24bde00e8647d8cbaf0b50463e","scene_sha256":"db633890383dca7cbbb20faba46b4cc8d22c70744f4b76229ddf2da9a8ad473d"}}'
_EXPECTED_ENTRIES_SHA = '23d59f68c41af7881be5018aa20120c320e8225b50caf692d62f00f6b69a4fec'
_EXPECTED_VARIANTS_JSON = '{"507487826":{"qianniaocheng":{"actor_sha256":"1b3539cf9d032e493023c2c6b8d30d3345c856688ea1230e567fc4d2593b856a","annotation_sha256":"db3539593747d63bac3aba5199f9092c549791aad51a55412ebeddf5bc018d73","label":"千鸟城","placement_sha256":"852acd67f91216f0c14c50d6ed3943c3471b5632932e1b16a592c84e2514fb0f","scene":"SunsetEnd2"}}}'
_EXPECTED_VARIANTS_SHA = 'c6e52a931dddcd7d409f62ae902c63ef3313b657df445671a50cf2be8d4ad90c'
# A convenience copy for fixture/manifest assembly, not the validation authority.
EXPECTED_ENTRIES=json.loads(_EXPECTED_ENTRIES_JSON)
EXPECTED_VARIANTS=json.loads(_EXPECTED_VARIANTS_JSON)

def _legacy_pins():
    return {'source_actor_sha256':SOURCE_SHA,'source_metadata_sha256':SOURCE_METADATA_SHA,
            'entries_sha256':_EXPECTED_ENTRIES_SHA,'variants':EXPECTED_VARIANTS}

def _pins_for(binding):
    """按 manifest 的 source_account_binding_hash 选 pin；没有就原常量。"""
    if type(binding)is not str or re.fullmatch('[0-9a-f]{64}',binding)is None:_fail('source_account_binding')
    row=_P.load_for_binding(binding)
    return _legacy_pins() if row is None else row

def _expected(map_id):
    if type(map_id)is not int or not 0<map_id<2**32:_fail('map_id_type')
    result=json.loads(_EXPECTED_ENTRIES_JSON).get(str(map_id))
    if result is None:_fail('map_not_allowlisted')
    return result

def _row_for(map_id,entries):
    if type(map_id)is not int or not 0<map_id<2**32:_fail('map_id_type')
    if type(entries)is not dict:_fail('entries_shape')
    result=entries.get(str(map_id))
    if type(result)is not dict:_fail('map_not_allowlisted')
    return result

# 2026-11-07 T18：同一张图的命名落点变体。变体只改 actor/placement 两件，
# scene/cache/scene-provenance 必须与主条目逐字节相同；variant pin 单独钉住。
VARIANT_NAME=re.compile(r'[a-z0-9_]{1,32}')
VARIANT_KEYS=frozenset(('scene','label','actor_sha256','placement_sha256','annotation_sha256'))
_VARIANT_SHA_KEYS=('actor_sha256','placement_sha256','annotation_sha256')

def _validate_variants(m,entries,pins):
    raw=m.get('variants',{})
    if type(raw)is not dict:_fail('variants_shape')
    if not hmac.compare_digest(_sha(canonical_json(raw)),_sha(canonical_json(pins['variants']))):_fail('variants_pin')
    for key,row in raw.items():
        entry=entries.get(key) if type(key)is str else None
        if type(entry)is not dict:_fail('variant_entry_missing')
        if type(row)is not dict or not row:_fail('variant_level_shape')
        for name,value in row.items():
            if type(name)is not str or VARIANT_NAME.fullmatch(name)is None:_fail('variant_name')
            if type(value)is not dict or set(value)!=VARIANT_KEYS:_fail('variant_shape')
            if value.get('scene')!=entry['scene'] or type(value.get('label'))is not str or not value['label']:_fail('variant_scope')
            for sha_key in _VARIANT_SHA_KEYS:
                sha_value=value.get(sha_key)
                if type(sha_value)is not str or re.fullmatch('[0-9a-f]{64}',sha_value)is None:_fail('variant_sha')
    return raw

def variant_names(map_id=None):
    """只读：某张图允许的变体名（没有返回空列表）。"""
    if map_id is None:return {key:sorted(row) for key,row in EXPECTED_VARIANTS.items()}
    return sorted(EXPECTED_VARIANTS.get(str(map_id)) or {})

def _entry_for(map_id,variant=None,entries=None,variants=None):
    """主条目或“主条目 + 变体 actor/placement pin”的完整 entry。

    entries/variants 为 None 时用内置 5b12 常量；否则用已通过 pin 校验的
    manifest 条目/变体（按账号生成时必须走这条）。
    """
    result=dict(_expected(map_id)) if entries is None else dict(_row_for(map_id,entries))
    if variant is None:return result
    if type(variant)is not str or VARIANT_NAME.fullmatch(variant)is None:_fail('variant_name')
    table=EXPECTED_VARIANTS if variants is None else variants
    row=(table.get(str(map_id)) or {}).get(variant)
    if type(row)is not dict:_fail('variant_not_allowlisted')
    result['actor_sha256']=row['actor_sha256']
    result['placement_sha256']=row['placement_sha256']
    return result

def _pairs(items):
    result={}
    for key,value in items:
        if key in result:_fail('duplicate_manifest_key')
        result[key]=value
    return result

def _utc(value):
    if type(value)is not str:_fail('utc_shape')
    try:d=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,OverflowError):_fail('utc_shape')
    if d.tzinfo is None or d.utcoffset().total_seconds()!=0:_fail('utc_timezone')
    return d

def validate_catalog_manifest(raw,now_utc=None):
    if os.environ.get(ENV_ENABLE)!='1':_fail('trial_disabled')
    if type(raw)is not bytes or not 2<=len(raw)<=131072:_fail('manifest_extent')
    pin=_B._pin(os.environ.get(ENV_MANIFEST_SHA))
    if not hmac.compare_digest(pin,_sha(raw)):_fail('manifest_environment_pin')
    try:m=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=lambda _: _fail('nonfinite_json'))
    except (UnicodeError,json.JSONDecodeError):_fail('manifest_json')
    if type(m)is not dict or m.get('schema')!=MANIFEST_SCHEMA:_fail('manifest_schema')
    if m.get('kind')!=KIND or m.get('classification')!=KIND or m.get('enabled')is not True:_fail('manifest_experimental_kind')
    now=datetime.now(timezone.utc)if now_utc is None else _utc(now_utc)
    if now>=_utc(m.get('deadline_utc')):_fail('trial_deadline')
    if type(m.get('apk_build'))is not int or m['apk_build']!=421589 or m.get('native_sha256')!=NATIVE_SHA256:_fail('manifest_native_build')
    for key in('source_actor_sha256','source_metadata_sha256','source_account_binding_hash','source_capture_record_sha256'):_B._pin(m.get(key))
    pins=_pins_for(m['source_account_binding_hash'])
    if m['source_actor_sha256']!=pins['source_actor_sha256'] or m['source_metadata_sha256']!=pins['source_metadata_sha256']:_fail('source_pin')
    for key in('complete_scene_initialization','loaded_scene_snapshot','full_connection_ready','capture_generated','trial_validated'):
        if m.get(key)is not False:_fail('false_readiness_required')
    for key in('source_current_live','destination_runtime_capture','native_default_spawn_reproduced'):
        if key in m and m[key] is not False:_fail('false_readiness_required')
    entries=m.get('entries')
    if type(entries)is not dict or len(entries)<32 or not hmac.compare_digest(_sha(canonical_json(entries)),pins['entries_sha256']):_fail('exact_catalog_entries_required')
    _validate_variants(m,entries,pins)
    # Canonical digest preserves int/bool distinctions as well as every frozen pin.
    return m

def _preserved_actor(source, target, source_obs, target_obs, position, map_id):
    if (len(source) != len(target) or source_obs._counts != target_obs._counts or
            source_obs._boundaries != target_obs._boundaries):
        _fail('actor_layout_changed')
    starts = []
    bit = 0
    for token in source_obs._tokens:
        if token.kind == 'raw':
            bit = (bit + 7) & ~7
        elif token.kind != 'bits':
            _fail('actor_token_kind')
        starts.append(bit)
        bit += token.extent * 8 if token.kind == 'raw' else token.extent
    bounds = {}
    for label, offset in source_obs._boundaries:
        bounds.setdefault(label, []).append(offset)
    def at(label, kind, extent):
        offsets = bounds.get(label, [])
        if len(offsets) != 1:
            _fail('actor_boundary_shape')
        index = next((i for i, start in enumerate(starts) if start >= offsets[0]), None)
        if index is None or (source_obs._tokens[index].kind, source_obs._tokens[index].extent) != (kind, extent):
            _fail('actor_field_shape')
        return index
    main_map = at('0x21dc8ec', 'raw', 4)
    origin = at('0x21dc858', 'raw', 4)
    history = at('0x21de7c4', 'raw', 1)
    velocity = at('0x21dca9c', 'bits', 8)
    changes = {main_map: struct.pack('<I', map_id), history+1: struct.pack('<I', map_id)}
    for i, value in enumerate(position):
        changes[origin+i] = struct.pack('<f', value)
        changes[history+2+i] = struct.pack('<f', value)
    for i in range(3):
        changes[velocity+i] = 127
    mask = bytearray(len(source))
    # 2026-11-06 好友传送：0x21dee4c 关卡状态头（raw2, level, resource tag, ?, bits9）可改成本图值，或保持源值。
    level_state = at('0x21dee4c', 'raw', 2)
    entry_tag = _expected(map_id)['netversion_u16'][0]
    optional = {level_state: bytes(2), level_state+1: struct.pack('<I', map_id),
                level_state+2: struct.pack('<I', entry_tag)}
    for i, (before, after) in enumerate(zip(source_obs._tokens, target_obs._tokens)):
        if (before.kind, before.extent) != (after.kind, after.extent):
            _fail('actor_token_shape_changed')
        if i in optional and before != after:
            if after.value != optional[i]:
                _fail('actor_level_state_mismatch')
            if before.kind == 'raw':
                mask[starts[i]//8:starts[i]//8+before.extent] = bytes([255])*before.extent
            else:
                for shift in range(before.extent):
                    byte, lane = divmod(starts[i]+shift, 8)
                    mask[byte] |= 1 << lane
            continue
        if i not in changes:
            if before != after:
                _fail('actor_unrelated_token_changed')
            continue
        value = changes[i]
        if before.kind == 'raw':
            if before.extent != 4 or type(value) is not bytes or after.value != value:
                _fail('actor_allowed_raw_mismatch')
            mask[starts[i]//8:starts[i]//8+4] = b'\xff'*4
        else:
            if before.extent != 8 or after.value != 127:
                _fail('actor_velocity_mismatch')
            for shift in range(before.extent):
                byte, lane = divmod(starts[i]+shift, 8)
                mask[byte] |= 1 << lane
    if len(source_obs._tokens) != len(target_obs._tokens) or any((a ^ b) & ~m for a, b, m in zip(source, target, mask)):
        _fail('actor_unrelated_bits_changed')

def _finite_vec(value,size,code):
    if type(value)not in(list,tuple)or len(value)!=size or any(type(v)not in(int,float)or not math.isfinite(v)for v in value):_fail(code)
    return value

def _num(value):
    return type(value) in (int,float) and math.isfinite(value)

def _level_start_transform(scale,quat,pos):
    x,y,z,w=quat;sx,sy,sz=scale
    rot=((1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)),
         (2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)),
         (2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)))
    rows=[[rot[i][0]*sx,rot[i][1]*sy,rot[i][2]*sz,pos[i]]for i in range(3)]+[[0.0,0.0,0.0,1.0]]
    raw=b''.join(struct.pack('<f',rows[r][c])for c in range(4)for r in range(4))
    return [struct.unpack('<f',raw[i*4:i*4+4])[0]for i in range(16)]

def _placement(placement,entry):
    if type(placement)is not dict or _sha(canonical_json(placement))!=entry['placement_sha256']:_fail('placement_pin')
    p=placement;xyz=_finite_vec(p.get('wire_position'),3,'placement_position');feet=_finite_vec(p.get('feet_position'),3,'placement_feet')
    if (p.get('scene')!=entry['scene']or p.get('map_id')!=entry['level_id']or p.get('classification')!='engineering_trial'or
        p.get('native_default_spawn_reproduced')is not False or xyz!=[feet[0],feet[1]+.5,feet[2]]):_fail('placement_scope_or_offset')
    if p.get('wire_position_float32')!=[struct.unpack('<f',struct.pack('<f',v))[0]for v in xyz]:_fail('placement_float32')
    prov=p.get('provenance')
    if type(prov)is not dict:_fail('placement_provenance_shape')
    kind=prov.get('kind')
    if kind==KIND_LEVEL_LINK:return _placement_level_link(feet,xyz,prov,entry)
    if kind==KIND_LEVEL_START:return _placement_level_start(feet,prov,entry)
    if kind==KIND_LEVEL_START_RUNTIME:return _placement_level_start(feet,prov,entry,runtime=True)
    if kind==KIND_USER_ANNOTATION:return _placement_user_annotation(feet,prov,entry)
    _fail('placement_engineering_evidence')

def _placement_level_link(feet,xyz,prov,entry):
    support=prov.get('support',{});portal=prov.get('portal_clearance',{});marker=prov.get('placementBoundary',{})
    normal=_finite_vec(support.get('signed_normal'),3,'placement_normal')
    if (prov.get('native_sha256')!=NATIVE_SHA256 or prov.get('apk_archive_directory_sha256')!=_B.APK_DIRECTORY_SHA or
        prov.get('source_member')!='assets/Data/Levels/'+entry['scene']+'/Objects.level.bin' or
        prov.get('entry_type')!='LevelLink'or type(prov.get('entry_object_index'))is not int or
        type(prov.get('transform_object_index'))is not int or prov['transform_object_index']!=marker.get('object_index')or
        prov.get('transform_encoding')!='serialized_marker_scale_quat_pos'or
        prov.get('transform_sha256')!=marker.get('sqt_sha256')or normal[1]<.65 or support.get('normal_y')!=normal[1]or
        prov.get('signed_normal')!=normal or support.get('footprint_radius')!=.22 or support.get('headroom')!=1.8 or
        type(support.get('footprint_y_span'))not in(int,float)or not 0<=support['footprint_y_span']<=.25 or
        support.get('full_capsule_collision_tested')is not False or
        prov.get('native_collision_verified')is not False or prov.get('native_spawn_selector_reproduced')is not False or
        prov.get('wire_origin_offset')!=.5):_fail('placement_engineering_evidence')
    for key in('source_member_sha256','geometry_manifest_sha256','entry_transform_sha256','transform_sha256'):_B._pin(prov.get(key))
    for key in('indices','positions'):_B._pin(prov.get('geometry_component_sha256',{}).get(key))
    samples=support.get('samples')
    if type(samples)is not list or len(samples)!=5:_fail('placement_footprint_samples')
    for sample in samples:
        if (type(sample)is not dict or type(sample.get('normal_y'))not in(int,float)or sample['normal_y']<.65 or
            type(sample.get('surface_y'))not in(int,float)or not math.isfinite(sample['surface_y'])or
            abs(sample['surface_y']-feet[1])>.25):_fail('placement_foot_support')
        h=sample.get('nearest_surface_above_gap')
        if h is not None and(type(h)not in(int,float)or not math.isfinite(h)or h<1.8):_fail('placement_headroom')
    if (portal.get('model')!='engineering_full_unit_cube_portal_AABB_vs_capsule_body_AABB'or
        type(portal.get('all_explicitly_enabled_LevelLink_count'))is not int or portal['all_explicitly_enabled_LevelLink_count']<1 or
        type(portal.get('minimum_body_aabb_separation'))not in(int,float)or not math.isfinite(portal['minimum_body_aabb_separation'])or
        portal['minimum_body_aabb_separation']<.5 or portal.get('capsule_radius')!=.22 or portal.get('capsule_height')!=1.8 or
        portal.get('required_margin')!=.5 or portal.get('native_trigger_shape_verified')is not False):_fail('placement_portal_exclusion')
    return xyz

def _runtime_level_start_evidence(feet,prov,entry,support):
    """v35：只接受可复核的运行时 beamo 上层地板证据，且必须如实记录 T1 失败。"""
    rg=prov.get('runtime_support_geometry');t1=prov.get('t1_static_collision')
    if type(rg)is not dict or type(t1)is not dict:_fail('placement_runtime_shape')
    if (rg.get('kind')!='runtime_beamo_static_world' or rg.get('source')!=RUNTIME_BEAMO_SOURCE or
        rg.get('runtime_manifest_sha256')!=RUNTIME_BEAMO_MANIFEST_SHA or
        rg.get('positions_sha256')!=RUNTIME_BEAMO_POSITIONS_SHA or
        rg.get('indices_sha256')!=RUNTIME_BEAMO_INDICES_SHA or
        rg.get('map_name')!=entry['scene'] or
        rg.get('geometry_kind')!='offline_beamo_augmented_candidates' or
        type(rg.get('triangle_id'))is not int or rg.get('triangle_id')!=support.get('triangle_id') or
        rg.get('triangle_component')not in('beamo_first_submesh_geometry_candidate',
                                           'baked_ordinary_terrain_candidate')or
        type(rg.get('object_resource'))is not str or not rg['object_resource'] or
        type(rg.get('object_index'))is not int or
        type(rg.get('mesh_source'))is not str or not rg['mesh_source']or
        not _num(rg.get('surface_y'))or abs(rg['surface_y']-feet[1])>1e-6 or
        rg.get('native_collision_verified')is not False or rg.get('calibration_verified')is not False):
        _fail('placement_runtime_source')
    normal=_finite_vec(rg.get('signed_normal'),3,'placement_runtime_normal')
    if normal[1]<.65 or normal!=support.get('signed_normal'):_fail('placement_runtime_support')
    ids=rg.get('sample_triangle_ids');samples=support.get('samples')
    if (type(ids)is not list or len(ids)!=5 or any(type(v)is not int for v in ids) or
        type(samples)is not list or len(samples)!=5 or
        [s.get('triangle_id')for s in samples]!=ids):_fail('placement_runtime_samples')
    if (not _num(rg.get('headroom_m'))or rg['headroom_m']<1.8 or
        not _num(rg.get('wall_clearance_m'))or rg['wall_clearance_m']<.45):_fail('placement_runtime_clearance')
    conn=rg.get('connectivity')
    if (type(conn)is not dict or type(conn.get('component_cells'))is not int or conn['component_cells']<1 or
        not _num(conn.get('radius_m'))or conn['radius_m']<=0 or
        not _num(conn.get('min_y'))or not _num(conn.get('max_y'))or
        not conn['min_y']-1e-6<=feet[1]<=conn['max_y']+1e-6):_fail('placement_runtime_connectivity')
    if (support.get('source_geometry')!='runtime_beamo_static_world_0.5m_footprint' or
        t1.get('model')!='t1_placement_engine_static_collision' or
        t1.get('source_geometry')!='apk_initial_static_collision_candidates_0.5m_footprint' or
        t1.get('upper_floor_present')is not False or
        t1.get('reason')!='footprint_missing_up_face_within_0.25' or
        not _num(t1.get('tested_y'))or abs(t1['tested_y']-feet[1])>1e-6 or
        t1.get('geometry_manifest_sha256')!=prov.get('geometry_manifest_sha256')or
        t1.get('geometry_component_sha256')!=prov.get('geometry_component_sha256')):_fail('placement_runtime_t1_miss')
    rows=t1.get('samples')
    if type(rows)is not list or len(rows)!=5:_fail('placement_runtime_t1_samples')
    seen=set()
    for row in rows:
        if type(row)is not dict or row.get('hit')is not False:_fail('placement_runtime_t1_samples')
        off=row.get('offset_xz')
        if (type(off)is not list or len(off)!=2 or any(type(v)not in(int,float)for v in off)or
            (round(off[0],6),round(off[1],6))not in LEVEL_START_FOOTPRINT):_fail('placement_runtime_t1_samples')
        seen.add((round(off[0],6),round(off[1],6)))
    if seen!=LEVEL_START_FOOTPRINT:_fail('placement_runtime_t1_samples')

def _placement_level_start(feet,prov,entry,runtime=False):
    support=prov.get('support');portal=prov.get('portal_clearance');marker=prov.get('placementBoundary');spawn=prov.get('native_spawn')
    bound=prov.get('scene_bounds');selected=prov.get('candidate_selection')
    if (type(support)is not dict or type(portal)is not dict or type(marker)is not dict or
        type(spawn)is not dict or type(bound)is not dict or type(selected)is not dict):_fail('placement_level_start_shape')
    if runtime:_runtime_level_start_evidence(feet,prov,entry,support)
    # 1. 场景/源资产 pin：Objects.level.bin 必须是本场景成员，几何 manifest 与分量都要 pin。
    if (prov.get('native_sha256')!=NATIVE_SHA256 or prov.get('apk_archive_directory_sha256')!=_B.APK_DIRECTORY_SHA or
        prov.get('source_member')!='assets/Data/Levels/'+entry['scene']+'/Objects.level.bin'):_fail('placement_level_start_source')
    for key in('source_member_sha256','apk_member_sha256','geometry_manifest_sha256'):_B._pin(prov.get(key))
    # source_member_sha256 必须与 scene-provenance 的成员 pin 相等（_scene_proof 会交叉核对）；
    # apk_member_sha256 是本地 APK 实算值；两者是否一致由布尔旗标显式记录，不能自相矛盾。
    member_flag=prov.get('apk_member_matches_scene_provenance')
    if (type(member_flag)is not bool or
        member_flag!=(prov['apk_member_sha256']==prov['source_member_sha256'])):_fail('placement_level_start_member_pin')
    for key in('positions','indices'):_B._pin(prov.get('geometry_component_sha256',{}).get(key))
    # 2. 源对象 pin：AvatarSetPos / Checkpoint / SpawnAvatars*，object_index 与 Marker 必须互相对应。
    entry_type=prov.get('entry_type')
    if type(entry_type)is not str or not any(entry_type.startswith(prefix)for prefix in LEVEL_START_ENTRY_TYPES):
        _fail('placement_level_start_entry_type')
    for key in('entry_object_index','transform_object_index'):
        if type(prov.get(key))is not int or prov[key]<0:_fail('placement_level_start_object_index')
    if (marker.get('object_index')!=prov['transform_object_index']or
        spawn.get('marker_index')!=prov['transform_object_index']or
        spawn.get('object_index')!=prov['entry_object_index']):_fail('placement_level_start_object_pin')
    if prov.get('transform_encoding')!='serialized_marker_scale_quat_pos':_fail('placement_level_start_transform_encoding')
    # 3. 序列化 SQT 与 4x4 变换：现场可重算，sha 与矩阵都必须自洽。
    scale=_finite_vec(marker.get('scale'),3,'placement_marker_scale')
    quat=_finite_vec(marker.get('quaternion_xyzw'),4,'placement_marker_quaternion')
    mpos=_finite_vec(marker.get('position'),3,'placement_marker_position')
    norm=marker.get('quaternion_norm')
    if (not _num(norm)or abs(norm-math.sqrt(sum(q*q for q in quat)))>1e-6 or
        abs(sum(q*q for q in quat)-1)>1e-5):_fail('placement_level_start_quaternion')
    if marker.get('normalized_quaternion_for_engineering_rotation')is not True:_fail('placement_level_start_quaternion')
    if marker.get('sqt_sha256')!=_sha(struct.pack('<10f',*(scale+quat+mpos))):_fail('placement_level_start_sqt_pin')
    trans=_finite_vec(prov.get('entry_transform'),16,'placement_entry_transform')
    if prov.get('entry_transform_sha256')!=_sha(struct.pack('<16f',*trans))or prov.get('transform_sha256')!=prov['entry_transform_sha256']:
        _fail('placement_level_start_transform_pin')
    _B._pin(prov.get('entry_transform_sha256'))
    if any(struct.unpack('<f',struct.pack('<f',want))[0]!=got for want,got in zip(_level_start_transform(scale,quat,mpos),trans)):
        _fail('placement_level_start_transform_mismatch')
    # 4. 朝上支撑 / 净空：5 点足迹 ±0.25 m，normal_y >= 0.65，头顶 >= 1.8 m。
    normal=_finite_vec(support.get('signed_normal'),3,'placement_normal')
    if (normal[1]<.65 or not _num(support.get('normal_y'))or support.get('normal_y')!=normal[1]or
        prov.get('signed_normal')!=normal or support.get('footprint_radius')!=.22 or support.get('headroom')!=1.8 or
        support.get('full_capsule_collision_tested')is not False or prov.get('wire_origin_offset')!=.5):
        _fail('placement_level_start_support')
    samples=support.get('samples');air=prov.get('air_fallback')
    if type(air)is not bool or type(samples)is not list:_fail('placement_level_start_air_flag')
    if air:
        # 空中落点：明确标记；没有地面支撑；仍然要在场景包围盒内、且离传送门 >= 0.5 m。
        if (samples or support.get('ground_support_available')is not False or support.get('triangle_id')is not None or
            support.get('footprint_y_span')is not None):_fail('placement_level_start_air_support')
        if type(prov.get('air_reason'))is not str or not prov['air_reason']:_fail('placement_level_start_air_reason')
        if spawn.get('ground_feet_position')is not None:_fail('placement_level_start_air_support')
    else:
        if (support.get('ground_support_available')is not True or type(support.get('triangle_id'))is not int or
            len(samples)!=5):_fail('placement_footprint_samples')
        span=support.get('footprint_y_span')
        if not _num(span)or not 0<=span<=.25:_fail('placement_footprint_span')
        seen=set()
        for sample in samples:
            if type(sample)is not dict:_fail('placement_footprint_samples')
            offset=sample.get('offset_xz')
            if type(offset)is not list or len(offset)!=2 or any(type(v)not in(int,float)for v in offset):
                _fail('placement_footprint_offsets')
            key=(round(offset[0],6),round(offset[1],6))
            if key not in LEVEL_START_FOOTPRINT or key in seen:_fail('placement_footprint_offsets')
            seen.add(key)
            if (not _num(sample.get('normal_y'))or sample['normal_y']<.65 or
                not _num(sample.get('surface_y'))or abs(sample['surface_y']-feet[1])>.25):_fail('placement_foot_support')
            if (not _num(sample.get('nearest_surface_above_gap'))or sample['nearest_surface_above_gap']<1.8):
                _fail('placement_headroom')
            if type(sample.get('triangle_id'))is not int:_fail('placement_support_triangle')
        if seen!=LEVEL_START_FOOTPRINT:_fail('placement_footprint_offsets')
        ys=[sample['surface_y']for sample in samples]
        if max(ys)-min(ys)>.25:_fail('placement_footprint_span')
    # 5. 级别起点落点必须落在记录的场景包围盒内（空中点也要）。
    lo=_finite_vec(bound.get('lo'),3,'placement_bounds');hi=_finite_vec(bound.get('hi'),3,'placement_bounds')
    if any(lo[i]>hi[i]for i in range(3))or any(not lo[i]<=feet[i]<=hi[i]for i in range(3)):
        _fail('placement_level_start_outside_bounds')
    sources=bound.get('sources')
    if (type(sources)is not list or not sources or len(set(sources))!=len(sources)or
        any(v not in LEVEL_START_BOUND_SOURCES for v in sources)):_fail('placement_level_start_bounds_sources')
    # 6. 传送门排除：已启用 LevelLink 触发体（4 m 半边长封顶）与胶囊体 AABB 至少分离 0.5 m。
    if (portal.get('model')!=LEVEL_START_PORTAL_MODEL or portal.get('capsule_radius')!=.22 or
        portal.get('capsule_height')!=1.8 or portal.get('required_margin')!=.5 or
        portal.get('trigger_box_cap_m')!=LEVEL_START_PORTAL_CAP or
        portal.get('native_trigger_shape_verified')is not False):_fail('placement_portal_exclusion')
    count=portal.get('all_explicitly_enabled_LevelLink_count');sep=portal.get('minimum_body_aabb_separation')
    raw=portal.get('raw_minimum_body_aabb_separation')
    if type(count)is not int or count<0:_fail('placement_portal_exclusion')
    if not _num(sep)or sep<.5 or not _num(raw):_fail('placement_portal_exclusion')
    if count==0:
        if (sep!=999.0 or raw!=999.0 or portal.get('nearest_portal_object_index')is not None or
            portal.get('separating_axis')is not None):_fail('placement_portal_empty')
    else:
        if (type(portal.get('nearest_portal_object_index'))is not int or
            portal.get('separating_axis')not in('x','y','z')):_fail('placement_portal_exclusion')
        if type(portal.get('nearest_portal_link_name'))is not str:_fail('placement_portal_exclusion')
    # 7. 出生点证据与选择记录：必须能追溯到同一个源对象/标记。
    if (type(spawn.get('source'))is not str or not spawn['source']or type(spawn.get('confidence'))is not int or
        not 0<=spawn['confidence']<=100 or type(spawn.get('ambiguous'))is not bool or
        type(spawn.get('air_anchor'))is not bool):_fail('placement_level_start_spawn')
    if _finite_vec(spawn.get('marker_position'),3,'placement_spawn_marker')!=mpos:_fail('placement_spawn_marker_position')
    if _finite_vec(prov.get('authored_anchor'),3,'placement_anchor')!=mpos:_fail('placement_anchor')
    offset=prov.get('feet_ground_offset_m')
    if not _num(offset)or abs(offset-(feet[1]-mpos[1]))>1e-6:_fail('placement_feet_offset')
    if air:
        if spawn.get('ground_feet_position')is not None:_fail('placement_spawn_ground_position')
    else:
        # 原生出生点地面投影：与 Marker 同一 (x,z)，高度只能在“向下 limit / 向上 2.5 m”内。
        ground=_finite_vec(spawn.get('ground_feet_position'),3,'placement_spawn_ground')
        limit=spawn.get('ground_drop_limit_m')
        if (ground[0]!=mpos[0]or ground[2]!=mpos[2]or limit not in(2.5,14.0)or
            not -limit-1e-9<=ground[1]-mpos[1]<=2.5+1e-9):_fail('placement_spawn_ground_position')
    if (type(selected.get('kind'))is not str or not selected['kind']or
        not _num(selected.get('distance_to_anchor_m'))or selected['distance_to_anchor_m']<0 or
        not _num(selected.get('selected_displacement'))or selected['selected_displacement']<0 or
        type(selected.get('available_candidate_count'))is not int or selected['available_candidate_count']<0):
        _fail('placement_level_start_selection')
    if (prov.get('native_collision_verified')is not False or prov.get('native_spawn_selector_reproduced')is not False or
        prov.get('native_executed')is not False):_fail('placement_level_start_native_flags')
    return [feet[0],feet[1]+.5,feet[2]]

def _placement_user_annotation(feet,prov,entry):
    ann=prov.get('annotation');eng=prov.get('engineering_check');portal=prov.get('portal_clearance');bound=prov.get('scene_bounds')
    if type(ann)is not dict or type(eng)is not dict or type(portal)is not dict or type(bound)is not dict:_fail('placement_user_shape')
    # 1. 标注记录原样嵌入并 pin；场景/地图号必须与目录项一致，来源必须是工作台。
    if prov.get('annotation_sha256')!=_sha(canonical_json(ann)):_fail('placement_user_annotation_pin')
    if (prov.get('native_sha256')!=NATIVE_SHA256 or prov.get('apk_archive_directory_sha256')!=_B.APK_DIRECTORY_SHA or
        prov.get('source_member')!='assets/Data/Levels/'+entry['scene']+'/Objects.level.bin'):_fail('placement_user_source')
    _B._pin(prov.get('source_member_sha256'))
    if (ann.get('scene')!=entry['scene']or str(ann.get('map_id'))!=str(entry['level_id'])or
        ann.get('source')not in('desktop-annotation','owner-live-position','recorded-capture-position')):_fail('placement_user_scope')
    # 2. 坐标：placement 的 feet 就是工作台几何坐标；工作台 Actor 坐标 = 几何 + (0,0.5,0)。
    geo=_finite_vec(ann.get('geometry_position'),3,'placement_user_geometry')
    act=_finite_vec(ann.get('feet_position'),3,'placement_user_actor')
    if geo!=feet or ann.get('offset')!=[0,0.5,0]or prov.get('wire_origin_offset')!=.5:_fail('placement_user_coordinates')
    if abs(act[0]-feet[0])>1e-6 or abs(act[1]-(feet[1]+.5))>1e-6 or abs(act[2]-feet[2])>1e-6:_fail('placement_user_coordinates')
    hd=ann.get('heading_deg')
    if not _num(hd)or not 0<=hd<360:_fail('placement_user_heading')
    # 3. 工作台自己的检查（渲染网格）：脚下必须是朝上的面。
    #    2026-11-06 另两种来源（如实标注，不冒充工作台）：
    #    owner-live-position = 哥哥在游戏里实际站着的点：同图、>=3 次新鲜采样、离散 <=5cm、采样值就是出生点 Actor 坐标；
    #    recorded-capture-position = 小光自己录制该图时的落点：同图、注明录制目录、录制位置就是出生点 Actor 坐标。
    if ann.get('source')=='owner-live-position':
        ev=ann.get('live_evidence')
        if (type(ev)is not dict or str(ev.get('map_id'))!=str(entry['level_id'])or type(ev.get('owner_uuid'))is not str or
            type(ev.get('samples'))is not int or ev['samples']<3 or not _num(ev.get('max_spread_m'))or ev['max_spread_m']>.05):
            _fail('placement_user_live_evidence')
        pos=_finite_vec(ev.get('position'),3,'placement_user_live_evidence')
        if any(abs(pos[i]-act[i])>1e-6 for i in range(3)):_fail('placement_user_live_evidence')
    elif ann.get('source')=='recorded-capture-position':
        ev=ann.get('capture_evidence')
        if (type(ev)is not dict or str(ev.get('map_id'))!=str(entry['level_id'])or type(ev.get('record_dir'))is not str
            or not ev['record_dir']):_fail('placement_user_capture_evidence')
        pos=_finite_vec(ev.get('position'),3,'placement_user_capture_evidence')
        if any(abs(pos[i]-act[i])>1e-6 for i in range(3)):_fail('placement_user_capture_evidence')
    else:
        chk=ann.get('checks')
        if type(chk)is not dict or chk.get('support')is not True or not _num(chk.get('support_normal_y'))or chk['support_normal_y']<.65:
            _fail('placement_user_studio_support')
    # 4. 服务器侧静态碰撞复核：结果原样记录；不合格时必须显式 user_override 并列出原因。
    reasons=eng.get('reasons');ok=eng.get('ok')
    if type(ok)is not bool or type(reasons)is not list or any(type(r)is not str for r in reasons)or ok!=(not reasons):
        _fail('placement_user_engineering')
    if prov.get('user_override')is not(not ok):_fail('placement_user_override')
    if eng.get('geometry_manifest_sha256')is not None:_B._pin(eng['geometry_manifest_sha256'])
    # 5. 必须在场景包围盒内；离已启用 LevelLink 触发体 >= 0.5 m（没有门时记 999）。
    lo=_finite_vec(bound.get('lo'),3,'placement_bounds');hi=_finite_vec(bound.get('hi'),3,'placement_bounds')
    if any(lo[i]>hi[i]for i in range(3))or any(not lo[i]<=feet[i]<=hi[i]for i in range(3)):_fail('placement_user_outside_bounds')
    sep=portal.get('minimum_body_aabb_separation');count=portal.get('all_explicitly_enabled_LevelLink_count')
    if (portal.get('model')!=LEVEL_START_PORTAL_MODEL or portal.get('trigger_box_cap_m')!=LEVEL_START_PORTAL_CAP or
        portal.get('capsule_radius')!=.22 or portal.get('capsule_height')!=1.8 or portal.get('required_margin')!=.5 or
        type(count)is not int or count<0 or not _num(sep)or sep<.5):_fail('placement_portal_exclusion')
    if (prov.get('native_collision_verified')is not False or prov.get('native_spawn_selector_reproduced')is not False or
        prov.get('native_executed')is not False):_fail('placement_user_native_flags')
    return [feet[0],feet[1]+.5,feet[2]]

def _scene_proof(proof,entry,observed,cache,wire,placement):
    if type(proof)is not dict or _sha(canonical_json(proof))!=entry['scene_provenance_sha256']:_fail('scene_provenance_pin')
    if (proof.get('schema')!='sky-catalog-partial-scene-inputs-v32'or proof.get('phase')!='finite_initializers_only_before_first_event_update'or
        proof.get('scene')!=entry['scene']or proof.get('map_id')!=entry['level_id']or
        proof.get('resource_revision')!=entry['resource_revision']or proof.get('netversion_u16')!=entry['netversion_u16']or
        proof.get('native_sha256')!=NATIVE_SHA256 or proof.get('canonical_roundtrip')is not True):_fail('scene_scope_phase')
    for key in('complete_scene_initialization','loaded_scene_snapshot','full_connection_ready','capture_generated','transport_used'):
        if proof.get(key)is not False:_fail('scene_false_readiness_required')
    missing=proof.get('missing_scene_inputs')
    if type(missing)is not list or not missing or any(type(v)is not str or not v for v in missing):_fail('scene_missing_inputs_required')
    counts=proof.get('wire_variable_group_counts')
    if type(counts)is not dict or any(type(v)is not int or v<0 for v in counts.values())or counts!=dict(observed._counts):_fail('scene_counts')
    artifacts=proof.get('artifacts',{})
    for key,data in(('cache',cache),('wire',wire)):
        a=artifacts.get(key,{})
        if a.get('sha256')!=_sha(data)or type(a.get('bytes'))is not int or a['bytes']!=len(data):_fail('scene_artifact_pin')
    prov=proof.get('provenance',{})
    if (prov.get('native_sha256')!=NATIVE_SHA256 or prov.get('apk_build')!=421589 or
        prov.get('apk_archive_directory_sha256')!=_B.APK_DIRECTORY_SHA or
        prov.get('scene_member_sha256')!=placement['provenance']['source_member_sha256']or
        prov.get('scene_member')!=placement['provenance']['source_member']or prov.get('runtime_capture')is not False or
        prov.get('native_record_comparison')not in(False,True)):_fail('scene_resource_provenance')  # 2026-11-06：True=APK 推导版本号已与真实录制核对一致

@dataclass(frozen=True,repr=False)
class CatalogBootstrapContext:
    account_binding_hash:str=field(repr=False)
    scene:str
    resource_revision:str
    level_id:int
    manifest_sha256:str
    native_sha256:str=NATIVE_SHA256
    apk_build:int=421589
    source_kind:str='saved_same_bot_capture_not_live_current'
    variant:str|None=None

@dataclass(frozen=True,repr=False)
class CatalogBootstrapInputs:
    _context:CatalogBootstrapContext=field(repr=False)
    _cache:bytes=field(repr=False)
    _wire:bytes=field(repr=False)
    _actor:bytes=field(repr=False)
    _manifest:bytes=field(repr=False)
    _source:object=field(repr=False)
    _source_metadata:bytes=field(repr=False)
    _placement:bytes=field(repr=False)
    _scene_provenance:bytes=field(repr=False)
    _actor_counts:tuple=field(repr=False)
    _netversion:tuple=field(repr=False)
    _seal:object=field(repr=False)
    def cache_bytes(self):return self._cache
    def scene_bytes(self):return self._wire
    def actor_bytes(self):return self._actor
    def __repr__(self):return '<CatalogBootstrapInputs private experimental asset trial; not capture>'
    def metadata(self):
        proof=json.loads(self._scene_provenance)
        return {'kind':KIND,'classification':KIND,'scene':self._context.scene,'level_id':self._context.level_id,
            'candidate_phase':proof['phase'],'source_kind':self._context.source_kind,'source_current_live':False,'destination_runtime_capture':False,
            'scene_bytes':len(self._wire),'actor_bytes':len(self._actor),'actor_unrelated_bits_preserved':True,
            'same_account_stored_source_revalidated':True,'complete_scene_initialization':False,'loaded_scene_snapshot':False,
            'capture_generated':False,'full_connection_ready':False,'trial_validated':False,
            'native_default_spawn_reproduced':False,'native_collision_verified':False,'native_trigger_shape_verified':False,
            'join_player_fields_policy':'engineering_identity_matrix_flag0_w0_not_native_defaults',
            'resource_runtime_revision_reconciled':False,'missing_scene_inputs':list(proof['missing_scene_inputs']),
            'transport_used_by_adapter':False}

def _validate(inputs,binding,scene,resource_revision,level_id,netversion,now_utc):
    if type(inputs)is not CatalogBootstrapInputs or inputs._seal is not _SEAL or type(inputs._context)is not CatalogBootstrapContext:_fail('inputs_type_or_seal')
    m=validate_catalog_manifest(inputs._manifest,now_utc);c=inputs._context;entry=_entry_for(level_id,c.variant,m['entries'],m.get('variants') or {});pins=_pins_for(m['source_account_binding_hash'])
    if (scene!=entry['scene']or resource_revision!=entry['resource_revision']or
        type(netversion)not in(list,tuple)or any(type(v)is not int for v in netversion)or tuple(netversion)!=tuple(entry['netversion_u16'])or
        c.scene!=scene or c.resource_revision!=resource_revision or c.level_id!=level_id or c.native_sha256!=NATIVE_SHA256 or c.apk_build!=421589 or
        c.manifest_sha256!=_sha(inputs._manifest)or c.source_kind!='saved_same_bot_capture_not_live_current'or
        type(c.account_binding_hash)is not str or not hmac.compare_digest(c.account_binding_hash,binding)or
        inputs._netversion!=tuple(netversion)):_fail('account_or_map_scope')
    for data,key in((inputs._cache,'cache_sha256'),(inputs._wire,'scene_sha256'),(inputs._actor,'actor_sha256'),
        (inputs._placement,'placement_sha256'),(inputs._scene_provenance,'scene_provenance_sha256')):
        if type(data)is not bytes or not hmac.compare_digest(_sha(data),entry[key]):_fail('payload_pin_'+key)
    if type(inputs._source_metadata)is not bytes or _sha(inputs._source_metadata)!=pins['source_metadata_sha256']:_fail('source_metadata_pin')
    source_obs=_B._source(inputs._source,json.loads(inputs._source_metadata),binding,m)
    placement=json.loads(inputs._placement);xyz=_placement(placement,entry)
    actor_obs=_B._actor(inputs._actor,level_id)
    if actor_obs._counts!=inputs._actor_counts:_fail('actor_counts_changed')
    _preserved_actor(inputs._source.actor_bytes(),inputs._actor,source_obs,actor_obs,xyz,level_id)
    record=_B._record(inputs._cache,inputs._wire)
    observed=check_scene_record(record,resource_tag=netversion[0],native_record_size=netversion[5],account_hash=binding)
    _scene_proof(json.loads(inputs._scene_provenance),entry,observed,inputs._cache,inputs._wire,placement)
    return SceneBaseline(scene,resource_revision,level_id,netversion[0],netversion[5],binding,record,_SCENE_SEAL)

def validate_catalog_inputs(inputs,*,account_uuid,scene,resource_revision,level_id,netversion,now_utc=None):
    return _validate(inputs,account_binding(account_uuid),scene,resource_revision,level_id,netversion,now_utc)

def build_catalog_inputs(*,manifest,scene_cache,scene_wire,destination_actor,account_uuid,source_metadata,placement,
        scene_provenance,verified_same_account_source,map_id,enabled=False,now_utc=None,variant=None):
    if enabled is not True:_fail('explicit_trial_required')
    m=validate_catalog_manifest(manifest,now_utc);entry=_entry_for(map_id,variant,m['entries'],m.get('variants') or {});binding=account_binding(account_uuid)
    actor_obs=_B._actor(destination_actor,map_id)
    inputs=CatalogBootstrapInputs(CatalogBootstrapContext(binding,entry['scene'],entry['resource_revision'],map_id,_sha(manifest),variant=variant),
        scene_cache,scene_wire,destination_actor,manifest,verified_same_account_source,canonical_json(source_metadata),
        canonical_json(placement),canonical_json(scene_provenance),actor_obs._counts,tuple(entry['netversion_u16']),_SEAL)
    validate_catalog_inputs(inputs,account_uuid=account_uuid,scene=entry['scene'],resource_revision=entry['resource_revision'],
        level_id=map_id,netversion=entry['netversion_u16'],now_utc=now_utc)
    return inputs

def _profile_record(inputs):
    c=inputs._context;m=json.loads(inputs._manifest)
    entry=_entry_for(c.level_id,c.variant,m['entries'],m.get('variants') or {});pins=_pins_for(c.account_binding_hash)
    xyz=json.loads(inputs._placement)['wire_position']
    spawn=struct.pack('<4f',*xyz,0.)
    matrix=struct.pack('<16f',*(1. if r==s else 0. for r in range(4)for s in range(4)))
    return {'schema':1,'kind':KIND,'classification':KIND,'apk_build':421589,'native_sha256':NATIVE_SHA256,
        'scene':c.scene,'resource_revision':c.resource_revision,'level_id':c.level_id,'netversion_u16':entry['netversion_u16'],
        'cache':{'sha256':entry['cache_sha256']},'actor_sha256':entry['actor_sha256'],'source_actor_sha256':pins['source_actor_sha256'],
        'source_kind':c.source_kind,'source_capture_record_sha256':inputs._source._context.world_generation.record_sha256,
        'manifest_sha256':c.manifest_sha256,'player_fields':{'spawn_hex':spawn.hex(),'matrix_hex':matrix.hex(),'level_flag':0},
        'join_player_fields_policy':'engineering_identity_matrix_flag0_w0_not_native_defaults',
        'candidate_phase':'finite_initializers_only_before_first_event_update','complete_scene_initialization':False,
        'loaded_scene_snapshot':False,'capture_generated':False,'full_connection_ready':False,'trial_validated':False}

@dataclass(frozen=True,repr=False)
class CatalogBootstrapProfile(LoadedProfile):
    _asset_inputs:CatalogBootstrapInputs=field(repr=False)
    def __repr__(self):return '<CatalogBootstrapProfile private experimental asset catalog; not capture>'
    def __getattribute__(self,name):
        if name in('record','cache','blob','binding','player_fields'):
            inputs=object.__getattribute__(self,'_asset_inputs')
            if type(inputs)is not CatalogBootstrapInputs:_fail('profile_inputs_type')
            c=inputs._context;m=json.loads(inputs._manifest);entry=_entry_for(c.level_id,c.variant,m['entries'],m.get('variants') or {})
            _validate(inputs,c.account_binding_hash,c.scene,c.resource_revision,c.level_id,entry['netversion_u16'],None)
            if (object.__getattribute__(self,'cache')!=inputs._cache or object.__getattribute__(self,'blob')!=inputs._wire or
                object.__getattribute__(self,'binding')!=c.account_binding_hash or
                object.__getattribute__(self,'record')!=_B._freeze(_profile_record(inputs))):_fail('profile_inherited_fields_changed')
        return super().__getattribute__(name)

def build_catalog_profile(inputs,*,account_uuid,now_utc=None):
    if type(inputs)is not CatalogBootstrapInputs:_fail('inputs_type_or_seal')
    c=inputs._context;m=json.loads(inputs._manifest);entry=_entry_for(c.level_id,c.variant,m['entries'],m.get('variants') or {})
    baseline=validate_catalog_inputs(inputs,account_uuid=account_uuid,scene=c.scene,resource_revision=c.resource_revision,
        level_id=c.level_id,netversion=entry['netversion_u16'],now_utc=now_utc)
    return CatalogBootstrapProfile(_B._freeze(_profile_record(inputs)),inputs._cache,inputs._wire,baseline._account_hash,inputs)
