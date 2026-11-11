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

_EXPECTED_ENTRIES_JSON = '{"1058995818":{"actor_sha256":"e9aa724765515dcb4c44b4b6ed89dda27517cafe8ad032f3fa700b5c9668eafb","cache_sha256":"e57fa147fad82c2ef3b60c1b29cd95654ba79101dba7afb89e63336f4ac734b2","label":"\u73cd\u73e0\u6d77\u57df","level_id":1058995818,"netversion_u16":[48196,49307,23536,0,0,25536],"placement_sha256":"6fdddd46e7695eb9b9e7ec5578c907d84d974be61458b6c542f56d7f48af291b","resource_revision":"apk-v76318-421629","scene":"Dusk_PearlSea","scene_provenance_sha256":"b89c496cdcce580c5ea9207046c04f7ba2ca1abac8e4bc83014da0cedc6c6c9d","scene_sha256":"92245bfd67776c5ed3469b134b8b57642e5c09c9120f57374307bde9ee27fb31"},"1104763408":{"actor_sha256":"cfdc1218528200fc21080085d8cb187f525cf7ab5a93ebb9a333bb550139ee9f","cache_sha256":"ec93d32274efd90a1911418190a4b76670cf7013c7c24efab49a382510b7d218","label":"\u4ed9\u5883\u8336\u4f1a","level_id":1104763408,"netversion_u16":[4688,49307,23536,0,0,25536],"placement_sha256":"e05f3fd2e866da9391ec5eb5cb4972a035401bbf27369ebea868af079b1886a2","resource_revision":"apk-v76318-421629","scene":"MainStreet_Cafe_Wonderland","scene_provenance_sha256":"038815d5a899979c789aebf7a73f01b385ac758e344301efdf0ea79c62cf5796","scene_sha256":"d5f01b3241cf1a25c6921e2ffb4bd50db53fffb04d4b56c1c9db76a4e8b7bc8b"},"112616686":{"actor_sha256":"f3640659ef9c55f79d70599a22b07a0082ab062186eea240e798e9f38da512c4","cache_sha256":"9f2cd9dcb62ac63d3ec978e88c7871c8e242f26fe4c04498eed65e1269289601","label":"AR\u9ea6\u7530\uff08\u5185\u90e8\u573a\u666f\uff09","level_id":112616686,"netversion_u16":[46117,49307,23536,0,0,25536],"placement_sha256":"d6254749bdbc14e16d6b510c80529526e5d0d49e1e1d10d9e0b77ada387fc30e","resource_revision":"apk-v76318-421629","scene":"AR_WheatField","scene_provenance_sha256":"696c65e6e8be22b841ddd24403431baf3daeb79f850792a268d02bc01a38c7d7","scene_sha256":"5e4d6ba4b1182488bad9ebfd870ab6441112c9e7ae255aac59a7013793e14d8e"},"1147491976":{"actor_sha256":"5fab02035c4137c94aa1c094ef1c5ebbe13996b13049c49da3a5d1f8f4b8e76f","cache_sha256":"5e27429c7064880618c022865bb7125e3744efef10717ad0b2b1828847c75db3","label":"\u8fb9\u9672\u8352\u6f20","level_id":1147491976,"netversion_u16":[4310,49307,23536,0,0,25536],"placement_sha256":"c9b705039c8a7044525c5277ca1e2b4263a43e96c9d09a93b25e7d4c64006457","resource_revision":"apk-v76318-421629","scene":"Dusk","scene_provenance_sha256":"b5e674efcfbab74b9a746cba51b769c9ca0608f0b577449ae691f2e11041d2d9","scene_sha256":"828cfa91d55a54a77ae00f965730c279778edeb5673a23f8f34ebbbf336e35ea"},"1152668600":{"actor_sha256":"971c6bf75a685198500ea6b9eec1fcfe2c095beb7b265fb87c582863b5064e03","cache_sha256":"cb5cfe9c7ff99ca3fc463f8e99cc26d42541cf75742a56327a54b525aefa5333","label":"\u8352\u5e9f\u795e\u6bbf","level_id":1152668600,"netversion_u16":[22572,49307,23536,0,0,25536],"placement_sha256":"83747a0192aafeb2481031fbebfbb661ade72714ba5491f1c1ead21b3bc1b14b","resource_revision":"apk-v76318-421629","scene":"Dusk_HiddenTemple","scene_provenance_sha256":"f4d04045f718a98d0662154132bd2d4f80def1c2ac9354452c64804edb967473","scene_sha256":"99f5506292ea6a98003957935ae88c9455bbfb0740592efe7626898a70063bbc"},"1185708672":{"actor_sha256":"acc3db0ceda9c6ae310935555daa455141810c36ab1469c65710c1b10130221c","cache_sha256":"330c5461876e0ed689accdd18c8c980e2ab285580c32b408b8788c41d0efa9f0","label":"\u59c6\u660e\u5c4b\uff08\u5929\u7a7a\u573a\u666f\uff09","level_id":1185708672,"netversion_u16":[61885,49307,23536,0,0,25536],"placement_sha256":"004fbb88a1bdaab7b88e92a77c8a61876e2c4109f0cd1eee9f2cecb6fbd07f3d","resource_revision":"apk-v76318-421629","scene":"Night_ValleyHouseSky","scene_provenance_sha256":"4535331bad7326987b7f6111e3785498011f0f9f81fa327a1785f223df3ef81e","scene_sha256":"17f5a6071ac9aa3be472617873ae2f9b45256248187f7a277ff5da33d0dd0510"},"1190972738":{"actor_sha256":"747e6ef54ea8fe610db853eeb34c59c7b26ca93fe728f7acb52e18859423b5a4","cache_sha256":"7fdbe64a357066e3af3890881e92c3bfb877397fb9bc769cebbe008a2e4e203b","label":"\u4e91\u91ce\u795e\u6bbf","level_id":1190972738,"netversion_u16":[12644,49307,23536,0,0,25536],"placement_sha256":"aa5d9eb89ab2e658ba6e2272c7664eccc761ac27ded3f460ab617702511d1341","resource_revision":"apk-v76318-421629","scene":"DayEnd","scene_provenance_sha256":"198c5f1bf7e7e502babd8c267150e6f1e81900861d1aaedb29d26d2247a0a63f","scene_sha256":"640f6cbd7076134a59326c5a3795e5e1341492d4aebd7dab5f5c925aba86599e"},"1241316521":{"actor_sha256":"930ae170e2a7e2b2297c838cc5fcef90411129b2449805004cd28c144dde48e6","cache_sha256":"f60e1e6c9944ebd96c473ab5a3df36eb418a4c51ef2d0fc2c2d627e8b7216cb9","label":"\u706b\u4e4b\u8bd5\u70bc","level_id":1241316521,"netversion_u16":[41480,49307,23536,0,0,25536],"placement_sha256":"cc76e68038778040770314f73de2d5c85b54722105cf8b4240b0a4982958ed34","resource_revision":"apk-v76318-421629","scene":"Dawn_TrialsFire","scene_provenance_sha256":"08a37a80343c8c4a6b4123e3f0e3deab5696901643775e598b13cbd5634ae3ce","scene_sha256":"a72fe6745d81d6bf5c3c00c8f7fe1730d8c4a76ecad4c10b582ae777a2c4c767"},"1274288158":{"actor_sha256":"d7dc04ab7c3acc13986e869dadd576f5fd15152746b6483a5e7e08054ca1924b","cache_sha256":"58424d9c16683914f16234d8cfb412e3701d3b066c43d19a3ec180969b1cee45","label":"\u81f4\u68b5\u9ad8\u5b63\u4e3b\u9898\u5c0f\u5c4b","level_id":1274288158,"netversion_u16":[37837,49307,23536,0,0,25536],"placement_sha256":"dab3d50254a6c998bc4e0ca2a89c151125a21b3805eaab070220c50acf566c45","resource_revision":"apk-v76318-421629","scene":"Nest_Night_GO_Town","scene_provenance_sha256":"1e4a63b8665abfc4d7d91651120e00b6eaf03e6066c506e12f42abac89a953d9","scene_sha256":"72b800f85320e538d4fb3d31a46eb54850c5f516d9538d7fb985e3c06ea7eaf6"},"128844448":{"actor_sha256":"9ead505f706f1f5e0ca9d05dc9ba6a1dc18b07729357028eb3b86c934ff0e7fa","cache_sha256":"7872f3c71a2731b01cdd905c86ac2cd997c0f5984d8c1b7af86410ed0e4e633f","label":"\u96e8\u6797\u795e\u6bbf","level_id":128844448,"netversion_u16":[6082,49307,23536,0,0,25536],"placement_sha256":"841c80b27bfeee00df1e1836ad910daaf2348e17c7630897662c3a435b44f54f","resource_revision":"apk-v76318-421629","scene":"RainEnd","scene_provenance_sha256":"f64d3bc1d195f104e9f5f7f6c7d4bd646873ecda3357c4d12a0e1bbc7fe098fc","scene_sha256":"6c74c24adde26e75b7c1be3fc398c758d57aaa8ecd60ecfbb2f8acd65f9f387e"},"1323747701":{"actor_sha256":"b20b9181c869f7e443c904598612fa9f5f97facb1ae174a578a07449a4684903","cache_sha256":"a9c3f37916a2f7ddafbc51e22a57eac2e020203f560173daff88710a8415e927","label":"\u97f3\u4e50\u5927\u5385","level_id":1323747701,"netversion_u16":[45743,49307,23536,0,0,25536],"placement_sha256":"ee18c945f0243cbefdd4543a58066f562fb8c95f9dfb1ef78e2f2ff05caf12d1","resource_revision":"apk-v76318-421629","scene":"SunsetVillage_MusicShop","scene_provenance_sha256":"34ce69fd9425f41946c6f8130fa629863e3eab26fc95d72ea1649c685db94ebc","scene_sha256":"e2b1ea6750bf66435f085cc95672800750633fa9a4b86d5fbf5ed79df8c03364"},"1444818677":{"actor_sha256":"0c76ff3e37af2b03c2c61ad57fc26f9ecc299fa246ea43b26d633e27e50c4c85","cache_sha256":"51b7e05b5fbbf10f08bf9385daa54fac528f6e7e01b42a38401131e1bd0ed82f","label":"\u751c\u70b9\u5de5\u574a","level_id":1444818677,"netversion_u16":[17330,49307,23536,0,0,25536],"placement_sha256":"b725797063d341d784ebca42921ce141c8b9ea8e7e9af2f22080ff2edbeec0e4","resource_revision":"apk-v76318-421629","scene":"MainStreet_Cafe","scene_provenance_sha256":"3deadd4679153ed9556b8f1f02826a1db81c2a39a6554b76b56918e9999f99ad","scene_sha256":"eeb541a68ffc970e7c5358c3841d68b37529c1cbaba99a0f4f54842d4af8b0b1"},"1482972144":{"actor_sha256":"e92271679ef720fe3365070d57e0e391f1a472f4793f85b5da041aa34842768a","cache_sha256":"0f1c0bdf5406ba6f66f2f3e00349c4ae00298d80d9864d8da5b7f10836a94c0c","label":"\u5171\u73a9\u7a7a\u95f4","level_id":1482972144,"netversion_u16":[2393,49307,23536,0,0,25536],"placement_sha256":"10fd681617b56b418f98585c5fff203e357f71e5f41103df0504b49bfc6de5c5","resource_revision":"apk-v76318-421629","scene":"VoidSharedSpace","scene_provenance_sha256":"9c43f9b38182e317801e5f395dbf721ba7cfaaa4ca191275f786d7bc8c6e1db7","scene_sha256":"f92b4179b7d801036f14cb5822d6b1715c4cdfc1a9d855b31e11a5d62c473f59"},"1509719447":{"actor_sha256":"cebc4d2775833aa4bdb6063bd8562e64e280b48071466b955732ec6d8260a2cb","cache_sha256":"5cec8d093171184541a84470d6980fbe4c7de71f3908c718a6704ce51bee1f6e","label":"\u5c0f\u9547","level_id":1509719447,"netversion_u16":[31696,49307,23536,0,0,25536],"placement_sha256":"31944d55316d6c39faf8250f6409ea28ad9edc0126052887f2d310a38113f761","resource_revision":"apk-v76318-421629","scene":"MainStreet","scene_provenance_sha256":"e340a66532d834a909020f7ed3ec9f7b3af8ec5e556d5b10e933729ee7241767","scene_sha256":"d1da52e5d92346ec80f594b04c43017fc0156d19d3e87554349f38e3a9e2439b"},"1559613961":{"actor_sha256":"8fb57c8b4b3dfa298fb7573ddbe4a95914c0057f078e0c30b9149f70fd3448d8","cache_sha256":"60815e767715e5cdb198972ffdefc379554e36ddebe5e5a195d253357bd3c0a7","label":"\u97f3\u4e50\u9910\u5385","level_id":1559613961,"netversion_u16":[26450,49307,23536,0,0,25536],"placement_sha256":"d57fcbbf3f12e496dece4c21bb0589f0a43c91643a8c8eff37d8cabd339680a3","resource_revision":"apk-v76318-421629","scene":"MainStreet_ConcertHall","scene_provenance_sha256":"ac73b8a7b39b93538f0e2145e7f42913bba6f197b136d6cf9ce22c54fa43038c","scene_sha256":"83d98767360544f1667b767ad9ea15fd382685445f9de7fcff79368a013e3e2e"},"1561318939":{"actor_sha256":"083c22d18542a249ab06f0af3609ae150879dd1ca2eb51a376c2bdb8e4a6c790","cache_sha256":"c25f169a8ecd4a62ea0a1fadc3bdc72ca9d94657722a0978f9482cf82e4d47a0","label":"\u7e41\u534e\u738b\u90fd","level_id":1561318939,"netversion_u16":[22227,49307,23536,0,0,25536],"placement_sha256":"3386ba093001732c976c3a5e87178bfc0f91d08c5aa207c4c32c11fa30e41998","resource_revision":"apk-v76318-421629","scene":"DuskMid_Past","scene_provenance_sha256":"1abb41b8d43db3fe74d4a87adf97c60f398833d0a4f339772dcaf5be9d26fdcf","scene_sha256":"760f30ba0909a7ff390f2594805d32f9beb7d4adf3d6b9b37d00c628afc2b776"},"1563891948":{"actor_sha256":"ebfc88dbc621ca2ad46e40bed7893234aa893b60ea641ad40ff3e970ae4d58be","cache_sha256":"9d0acf8215ad1cde5778d58c7bb5975f1779633220869bb4c507e5aeea27566b","label":"\u670d\u88c5\u5e97","level_id":1563891948,"netversion_u16":[21372,49307,23536,0,0,25536],"placement_sha256":"166af9b1dbb6dc826fe034077355fce97b39e327597ae4c9c2640532af47c026","resource_revision":"apk-v76318-421629","scene":"MainStreet_ShopOutfits","scene_provenance_sha256":"d6dc89eba70b968643e455abb50d26d5cf170a683034c948fe4022837ee3ed91","scene_sha256":"4697c5478aec4862e9bbfa74b55bc165048ca97bf89e33a91e65a6a393ff2cc6"},"1597085778":{"actor_sha256":"af59fb0ad24aacbc167c08b0e60908249806b5db20151083adcd9d5971bd0523","cache_sha256":"51929e072b343b3c36c707fa5983b836509e6c456296ccf39fded1243bfc62f8","label":"\u8fdc\u53e4\u6218\u573a","level_id":1597085778,"netversion_u16":[64686,49307,23536,0,0,25536],"placement_sha256":"f6caf639914b529327dd66d21e241946639cad3b6af237dca2e9e19d415006fa","resource_revision":"apk-v76318-421629","scene":"DuskMid","scene_provenance_sha256":"7eb55b79f3fa52e5fef48b8e2c474f205e924f1f8eb4d7bf0cced5dce6f00636","scene_sha256":"daab0834d72716ee27415763aaf25b5c3f82232ef387f9d8c635b4ca8dd2fa56"},"1612139937":{"actor_sha256":"76d0b2f0e8b6fab0a0d6c83b2e2dbc5001466cd910720eef8edfed3d33752c96","cache_sha256":"ba599e5a3fbf8dc220af9b241bd0ef8842fbbda72dd143d1fe236ea15e319281","label":"\u5706\u68a6\u6751\u5267\u573a","level_id":1612139937,"netversion_u16":[56922,49307,23536,0,0,25536],"placement_sha256":"b0b338d5e7312521f7b55fc46fcf24c91b5eec14ce3b643a3c2f23bdcd1c04cb","resource_revision":"apk-v76318-421629","scene":"Sunset_Theater","scene_provenance_sha256":"f2fef608fffbaa5def4f5b4cab1653f43184dd3597031ad76456c3fa6af7e500","scene_sha256":"e36770e647a8edd086062623bf0672cc51c1d655735214e52c38836439e38e85"},"1638008359":{"actor_sha256":"d2393526caaf56d2a30c0fc11d42f2e6d71fae06ca059d1d91676314edfa9c53","cache_sha256":"d22b86feb1c60e35255c93e1d0603bdd942ea42d5f52bd4c773d24640151ceb8","label":"\u971e\u8c37","level_id":1638008359,"netversion_u16":[50293,49307,23536,0,0,25536],"placement_sha256":"e858096583e9b7e58327fdf4006be5a7d2acbfbe74922179976950bf07648756","resource_revision":"apk-v76318-421629","scene":"Sunset","scene_provenance_sha256":"6b1d581e0e19fece355f25f5746322fa0648386499ff4529021c01a74ac2db42","scene_sha256":"99e06105160aab0e167baed32fb187009b9f0814eb2412677bdf2f21f430e190"},"164626931":{"actor_sha256":"6a33b5ddfa027e165a1b73a40fc6a108859aa96e81c2930e90d2b0e2f47b4e08","cache_sha256":"5684ea79113ae2e26283f2dac9969109c0fcca6c11c03e1301ca4187c64a23bd","label":"\u96e8\u6797","level_id":164626931,"netversion_u16":[44931,49307,23536,0,0,25536],"placement_sha256":"14ed7602a75be106ef0db3d2f6ecea9b747f1fed3b7a69b8e83a971a61e62819","resource_revision":"apk-v76318-421629","scene":"Rain","scene_provenance_sha256":"b17d87bdbd09a5722ee4aa059ac7d7b3d95f49e36e1f990e699affe155072204","scene_sha256":"77a872adcb45cb6783b05fc4be621e14c4a38912a750152a465285bf59e3b265"},"1649439303":{"actor_sha256":"c6cb564441feeb5f903cecc059895767ed4af31b52d94dbdf7f6239113d4fd06","cache_sha256":"a9d53fdffc48a0c7e7de2a3b866bde558ec1dd29f4a6f4e707122d4aafa0ff5b","label":"\u6668\u5c9b","level_id":1649439303,"netversion_u16":[45075,49307,23536,0,0,25536],"placement_sha256":"48a74f505fa1c62c549fc19ef8dc6e53526e383a96c2b2898df4dadc5b7afe88","resource_revision":"apk-v76318-421629","scene":"Dawn","scene_provenance_sha256":"07c890ea67c7692060847e18a865760408568ba5321a498a60c0b60a8f129180","scene_sha256":"a92e6655bddac3f3f16d65ace97790c7faf4b2a2373c20075e7bf3f0441cd3b5"},"1661095040":{"actor_sha256":"34433dbae387c6bb76d7f2827242d7aa3f70ad51fbace7e502171c828b1cfeb2","cache_sha256":"f78be25ba1fb2e73192a56a6ce62db921f433a2ecccfb084f4b7a5a2ea0757d2","label":"\u6b27\u82e5\u62c9\u00b7SoftInside\u6b4c\u66f2\u573a\u666f","level_id":1661095040,"netversion_u16":[3991,49307,23536,0,0,25536],"placement_sha256":"459a3594f4380ea2b5d04691b101b781d75e59b155ce49d1877fb887ee471a73","resource_revision":"apk-v76318-421629","scene":"Event_Arr_SoftInside","scene_provenance_sha256":"29784976473c6908512884e75d1869be7f8210d2bd6a3310c2dd0a7adf9426bd","scene_sha256":"acbca7cde3063f4274a849dd1870cfe2a1caffb4b12a3b7978d087183e483ae6"},"1665365959":{"actor_sha256":"9f1e1bf47d96f6860c4826caba537eac915e64dc4e4011d2069b4fc8aa7fb66c","cache_sha256":"154139d6e3621ac4fb0231eb924fce40c7b2cb517450b44df2307faeaae2b226","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u7530\u91ce\u573a\u666f\uff09","level_id":1665365959,"netversion_u16":[46218,49307,23536,0,0,25536],"placement_sha256":"53277ee24c210830cadab2afa913f6465dda0421fc407a1ebfaffa52eac60977","resource_revision":"apk-v76318-421629","scene":"Night_GO_Field","scene_provenance_sha256":"dab189ef4db89b847c458199135b6f1c60f874aef4317dfd18ae4d2e8825cf76","scene_sha256":"694ad5f4d3e21309183c36251ff2bbde9756f45dd3027bd487d990a493e553e6"},"1694829989":{"actor_sha256":"a484e12ce0744949bff2db68efe2e35df8ee658e1665f2feac3cc16893bb9874","cache_sha256":"ae3cf0470682c8ba02cb3059ba3beb302d57d371a6bd12655af1285f818973dc","label":"\u6708\u7259\u7eff\u6d32","level_id":1694829989,"netversion_u16":[49660,49307,23536,0,0,25536],"placement_sha256":"a250387af2d354771e99029cf269bc9adbaab73af6bb7138a123cf38015df140","resource_revision":"apk-v76318-421629","scene":"Night_PaintedWorld","scene_provenance_sha256":"58979027f6dc5f2426565ff12e8a7797edcba5b397bbfa17ed5bf504ff1a7c27","scene_sha256":"f68732fefd74f068afe00ff894e4d43f4c81e89693187899347a8694561d294d"},"1705189686":{"actor_sha256":"71bf16796c5c2b54cb771747391d243ff042148b42e4dd60fb73296aaace389b","cache_sha256":"cfc7b14281508bd09983a8a7b5c9ca53480781375c8791f66f97657cda635625","label":"\u66b4\u98ce\u773c","level_id":1705189686,"netversion_u16":[24346,49307,23536,0,0,25536],"placement_sha256":"9c66d7ad5c5e0e8cbb11cb86a23eaf84c1993815afc053e34ca193154986da86","resource_revision":"apk-v76318-421629","scene":"Storm","scene_provenance_sha256":"5acc7d6cde23258c6361381c46b1931a987a50efe905ccf749424b00ed8ae2f6","scene_sha256":"8cba8ead4204fdb11755088d49117d38bd4929b69e055421e843a8b3139c82ae"},"170656205":{"actor_sha256":"eab5067b31144a35f11bc72cc07c88dd684922d6c3d2e448502be111ee07d109","cache_sha256":"2852694ae47a8a6fe485bd93d85313308a422860428eb138eac756f8be0024d9","label":"\u5931\u843d\u65b9\u821f","level_id":170656205,"netversion_u16":[17784,49307,23536,0,0,25536],"placement_sha256":"cb5bede50aaf5dbc35c96e1d42f3c57bd9f245544a79ef064881328f9d4d97e7","resource_revision":"apk-v76318-421629","scene":"DuskOasis","scene_provenance_sha256":"f2febed481165379c4431b64789e6140b26c782e9247c56d2b51d09e17b62832","scene_sha256":"c66d858c549d8dc028d7ce900c7088a92e7849d9d15cf29294647ca60b44d5f5"},"1737684623":{"actor_sha256":"a2fabcd93f68eef1697bab88e0e63531262911165adb25adbd4aa6ddf929a776","cache_sha256":"41071029483a11ac9ee941c89016d9c1e02ac774ee5a847be2d143357b9b6d8b","label":"\u6c38\u591c\u4e4b\u5730","level_id":1737684623,"netversion_u16":[61909,49307,23536,0,0,25536],"placement_sha256":"57f95a2f41ba3e9e935ffd707b7df40a4f9f7a2c6769218bd29da356fc465381","resource_revision":"apk-v76318-421629","scene":"Event_WerewolfGame","scene_provenance_sha256":"16250522ba71e991cfb975273677555a8254d7c70ca0dc5f6b0ebae960184e5c","scene_sha256":"30e3b7ccea9658ddaa51ee23279c6493ac39fa91b6919cb2cf711fcaeff81d6d"},"1759178769":{"actor_sha256":"2b89a4480b519dccb85462dd1fdd53e347b0f312f8da223fc43239fe7b397583","cache_sha256":"dc0becdf57d8edb709c2dc8fc8bc611aa3d268934807ee30a53827e224b44869","label":"\u5706\u68a6\u6751","level_id":1759178769,"netversion_u16":[17529,49307,23536,0,0,25536],"placement_sha256":"923b3c9ad54dda19b86f669ef98e59ce3b1ccd66c6ca28cd15a2a6e0dd6ced55","resource_revision":"apk-v76318-421629","scene":"SunsetVillage","scene_provenance_sha256":"27c5297aded7fca5435dbea70de09c837998f3686352bc7c5586f7af5eadc565","scene_sha256":"c0e14827ee5b94f66f801eacbe943ca497506871394b5259458271398e44f6d5"},"1761965310":{"actor_sha256":"664e21be0260095d4ccff113bcbff1e583ea195e9ed9cae880425fb18ed0787d","cache_sha256":"ea8e99b2a832fbe197b234ec20d36eecfd4efa4b08be708d7efd2c10de01b63d","label":"\u98de\u884c\u6307\u5f15","level_id":1761965310,"netversion_u16":[62514,49307,23536,0,0,25536],"placement_sha256":"ff123a827305e9603969f630ad680dae81cca4446d5616fde4cff4e8b7244c90","resource_revision":"apk-v76318-421629","scene":"MainStreetFlyingIntro","scene_provenance_sha256":"9d3a7d83639d3a661a86f180d224646ebb5842a8ede0b52c5a44c0141519e581","scene_sha256":"a9e13587d8d78454df274be8e4f7c3b151d2b4d66dca084034586a8c9f6b14a8"},"1801452336":{"actor_sha256":"8fb112fb4b384cc459d61bb1767195d74997f9968a0b0a61928a4bbb2b74f65a","cache_sha256":"dc2e28bd03b45cf02da7ad78307e8d7fe5ca9fddc87da74bd9c1cf3549ed4690","label":"\u59c6\u660e\u8c37\u7684\u68ee\u6797","level_id":1801452336,"netversion_u16":[40183,49307,23536,0,0,25536],"placement_sha256":"bd4b9e15df57b00afee2fd22253e31051c7a77525f69c1b61e7dc84ad67818dc","resource_revision":"apk-v76318-421629","scene":"Night_ValleyForest","scene_provenance_sha256":"5ff62d33744737e3339d73c4e44f53e0110cdf14b5b47b4c010ba63fc2537c4a","scene_sha256":"bc25b14641698fbbe67d75f1867d7f6b6f0ed7f5b8f13ec0b9bbad4045ddccd3"},"1844499196":{"actor_sha256":"83e5cd62b010972d7398535740ffc7b35a733850c2bc1ba467836cb326d35d43","cache_sha256":"c2d22b3e8c94a81d0963e6716432ce6f9e5f41ec7cb276bad19cb0dd0495e4a6","label":"\u98de\u884c\u8d5b\u9053","level_id":1844499196,"netversion_u16":[30151,49307,23536,0,0,25536],"placement_sha256":"202fde56eca0437174587c70a8d303dc8bd0a905acf785cea042219414e2e29c","resource_revision":"apk-v76318-421629","scene":"Sunset_FlyRace","scene_provenance_sha256":"fcaabf4b1ab14a5fad6c9587acbbdd45861f5edc0752fee8f16078d9c4c339e6","scene_sha256":"a48033f78a44a872a45201e6e6125179e8e865013fe5b31a72bf8298a448cc29"},"1887730855":{"actor_sha256":"3cc3124c53da81673772d32e12dde465702f49bf6d04a8170d4542cd58fc94f8","cache_sha256":"f6f6c2552b006f1a29fc3387f166ee443f64c0cf43b5d905bb9996738577ecaf","label":"\u571f\u4e4b\u8bd5\u70bc","level_id":1887730855,"netversion_u16":[47323,49307,23536,0,0,25536],"placement_sha256":"b75abd7e9ecf0db81e560ff7b99565a39ead4d1817494910420dad5dd9b53aba","resource_revision":"apk-v76318-421629","scene":"Dawn_TrialsEarth","scene_provenance_sha256":"58e71783936083cda5720f03d20501e35babe97486845c0d41baa0266a48868b","scene_sha256":"3dab9d816606f62ffd0980c19b9eb040aa0677c3104dac192b9605a557cd8c7a"},"1936276829":{"actor_sha256":"4cf7de3b5061a551537cfc70123e6af8d767759c0342a7eded54b7401dc2fcf3","cache_sha256":"c37fd0fc253bc1576c9361af3239d1cf331b83969566e7c02c6f0aca5f2d0f7e","label":"\u6545\u4e8b\u7a7a\u95f4","level_id":1936276829,"netversion_u16":[46559,49307,23536,0,0,25536],"placement_sha256":"5a3143fef865db54a22529b66c364a2f46c3c90161d3b500922f9a2802b7835c","resource_revision":"apk-v76318-421629","scene":"MainStreet_StorySpace","scene_provenance_sha256":"ee8dc69f42e99d8eacbbe3657e935374b37db063060ff92efefdd0c9163466b9","scene_sha256":"8381845a24a5c56a0fc032d1e769b3aee566a567787aecaa06d5317d36ab5b4f"},"2050064391":{"actor_sha256":"99cd3ff4d2a15dc1cf90aa4752f90ea58e8b4ef546c82915d50a78274493a142","cache_sha256":"8ad42b3674593e6bbaf29b45fdca8f47a681f64d8d567260b65213da43669c6f","label":"\u98ce\u4e4b\u8bd5\u70bc","level_id":2050064391,"netversion_u16":[8243,49307,23536,0,0,25536],"placement_sha256":"bc2bc15a3fd9a6efcc690660468b7b9f77034ef6a6a8390678f89826717dc343","resource_revision":"apk-v76318-421629","scene":"Dawn_TrialsAir","scene_provenance_sha256":"61a079a0d747da6cf93387652d4a481c4990f3f1b44a7cfd64c2e246473667d8","scene_sha256":"21756a1f1a64c282b79cb8cc0b7d48ba885716fcffdbc6d43e3783be8501b289"},"2060214456":{"actor_sha256":"5a61eeb8ddd5266bbc4af05ce2f1b7bc3679a18537e7ec2d2ece076870dec708","cache_sha256":"17472ca9a45198706fc140e977970e784ca8b8e0b938e5213cc51ca96784ad51","label":"\u661f\u5149\u6c99\u6f20\uff08\u4e3b\u573a\u666f\uff09","level_id":2060214456,"netversion_u16":[61447,49307,23536,0,0,25536],"placement_sha256":"a6767c5c1cdb75c68c00270d9eda04bf0a8c5b135e0d4365137e01c55a39c2ea","resource_revision":"apk-v76318-421629","scene":"NightDesert","scene_provenance_sha256":"ae44642af95add4bf94df10d9cdcd44ff1abc52a04c9ff628b7d34500bcedf51","scene_sha256":"2ae87962c0f1e3db0abe071920107b7448f3bb37ac9c321338680ed622ff9b7e"},"2081768701":{"actor_sha256":"ff18f91d589e8e4ce67d56bcc3647e471be70a547601594c3b8af817127e6c57","cache_sha256":"e758ab9c26c1a8d5cf3811e334d6530d29fc37a55aecbdf2112e76ca241dd96e","label":"\u98ce\u884c\u7f51\u9053","level_id":2081768701,"netversion_u16":[39846,49307,23536,0,0,25536],"placement_sha256":"7b1c2846a3d45bedf44192a5bfb6053c287ac217b3dbd5238e0a6251fb07e82c","resource_revision":"apk-v76318-421629","scene":"Skyway","scene_provenance_sha256":"d2aae01262966c91ed7cc5ad44756da818de545e83322b5fdf515874303c90bb","scene_sha256":"60427d78aa746e088ea02d6cc69023f174ba9f5cd7b02f020fe835c50758a54c"},"2093785354":{"actor_sha256":"3e1cfe3315963d64eb36d3522da8f003c5e93f091de1335c210a989e8537d3a4","cache_sha256":"a0a45f314bfc5244920c33c703201721b4cf99139163c54c2b5a46bdd18a34f8","label":"\u6b27\u82e5\u62c9\u00b7TheSeed\u6b4c\u66f2\u573a\u666f","level_id":2093785354,"netversion_u16":[41045,49307,23536,0,0,25536],"placement_sha256":"67c7d3b66153061d4532ebcb4325c71a592d226a8d2a7f3d064d8a64477c55e8","resource_revision":"apk-v76318-421629","scene":"Event_Arr_TheSeed","scene_provenance_sha256":"eff4f48ce6832def31c2fd88105ac79ae2dbe1cfcc610ad1f5a13ba65fcdd942","scene_sha256":"07ab68dae47d8cb07f610a9d02802226b26c68a3a777697c634f820102699414"},"214074919":{"actor_sha256":"302c4f97a846052331a09c4b4450c4ddc7f8e94b04821656d5bdb667f93bae96","cache_sha256":"6dc90352625656daa60ca8e5bf786891fea5021ab9a96db7a11b9a9794093655","label":"\u6cea\u6c34\u7684\u4e16\u754c","level_id":214074919,"netversion_u16":[34451,49307,23536,0,0,25536],"placement_sha256":"88bd5a8549298648dc32d560cb5304bfca52be1d2cfbc3760df7986a1806c67f","resource_revision":"apk-v76318-421629","scene":"Night_InfiniteDesert","scene_provenance_sha256":"9b9053a266d87b436572bdfbd90374cea21d96c0716c559903a696775a9f14a1","scene_sha256":"9506f459eb847ebbeea7137e5e11bbcfb1eee9f43487d7d20aa341b487d3c236"},"2159642775":{"actor_sha256":"7a497fefbc28c2cecd10f10357d8eccf0077baf46135ab1d7fa9aeb64cd12a90","cache_sha256":"a0a1d14cc4f3357d0d2f267e20ca95cc407935a2ac26cf0cdfa7cdc25cc52ac3","label":"\u5bc6\u6797\u9057\u8ff9","level_id":2159642775,"netversion_u16":[25546,49307,23536,0,0,25536],"placement_sha256":"830224a06bef289c808fd9e811888810dfa37b2836e64ac3475d1ae2a3cb1987","resource_revision":"apk-v76318-421629","scene":"RainMid","scene_provenance_sha256":"7b1dcc43fd6c94103027076831c7d8d06feb4c42cc4986ba442ae26298e0276b","scene_sha256":"ad65bbf54bf7764277bd0875309fb72fc309debcc21716fd2f6df6692e9f26cc"},"217257707":{"actor_sha256":"1c57baa063c063cd28b1c6c23a952ce48c9b902eb25938dc2c3544f5a3531ea4","cache_sha256":"68cd667126f83593636a1fdc8c82583f22310ce86f0ff6afc7e56acd2d39d7d8","label":"\u59c6\u660e\u5c4b\uff08\u5730\u9762\u573a\u666f\uff09","level_id":217257707,"netversion_u16":[51358,49307,23536,0,0,25536],"placement_sha256":"ff7bc0c19698da086615eddc8844daca9ee9f007cca24f3636f7bdb5a9c086e0","resource_revision":"apk-v76318-421629","scene":"Night_ValleyHouse","scene_provenance_sha256":"4ae1a94d805b1be7ff6a04258461a4fd7be39907a3b0c74ad47690294a4a7b8c","scene_sha256":"f89a6352236added05b0be19a3dee5e5a035a3aa79860f1ea6ae2a6423510b63"},"2179549040":{"actor_sha256":"e4006ab5dfd3e77d7baafa36b46ebc96f0d60ae9fc5361d9f380e5f932d1d2cc","cache_sha256":"c645b19c14ce41b8308cf8476ab58a86465b1d6e5db4dc13f54affc79209c5e1","label":"\u971e\u5149\u57ce","level_id":2179549040,"netversion_u16":[37851,49307,23536,0,0,25536],"placement_sha256":"0fd98c4c12deae88f7c34813b48dcccab37ff293488ba3ac273def08c8762929","resource_revision":"apk-v76318-421629","scene":"Sunset_Citadel","scene_provenance_sha256":"a8b6e92e6b9e69a062061bd729254003b92dd091419220f9aef38dd72be665ab","scene_sha256":"d97a392c0bed24b2096976ed55484d0d2f4c56bb27a231fa933e75c50d3a1578"},"2199862534":{"actor_sha256":"a1642c0117ccaa946ad0361181fc40a4646fa8364b38e5cf6cd708038b174b7e","cache_sha256":"2dd9238f0a71520114e062a37208054fd8bc7eb2d6dfad6cb039d2b02f8f6860","label":"\u9690\u85cf\u5173\u5361","level_id":2199862534,"netversion_u16":[44969,49307,23536,0,0,25536],"placement_sha256":"e6a72028a7f48c26e96f013919d6b263d92bbb76590a79a0160e64dd88f69142","resource_revision":"apk-v76318-421629","scene":"Nintendo_CandleSpace","scene_provenance_sha256":"1ecd6a71369d528000696bae3c677d649ed90f5596cbfe083898e7ad3390db18","scene_sha256":"5134b64ce8fef5ff2c4b4573232feacd213bf8103d9e1bf9e7fca690725bf863"},"2251284635":{"actor_sha256":"b1d4b71c6b613269c92a262aa835263ab948c48d67793b99d3edc4a9b2f86e42","cache_sha256":"ea658c2f946c70c47e7b7166ec13ad491d1db51be3746ae7a6918c1a90e0d0a1","label":"\u661f\u8f68","level_id":2251284635,"netversion_u16":[51912,49307,23536,0,0,25536],"placement_sha256":"886a9942ff2b4bc1765be364a6719da7d30e38f760fb9cc26f430ee80a39cac8","resource_revision":"apk-v76318-421629","scene":"CandleSpaceEnd","scene_provenance_sha256":"1da1cd78994a73e221c4680bb2f4113b0e79331425c0027c6381221501a9c989","scene_sha256":"65634ef68a93a5d6df1a7b1d06cf873fa3e0c6929d33d7bff8b9901673582b8d"},"2267185542":{"actor_sha256":"c4d68601fef77805561cc0aa2ba9f8a7e4cd7c34369b43219d1c3869831f7137","cache_sha256":"8b48c5abd73c85b9c3670fc0041b4a25e37ccc85eed8b8552d978557e68fedda","label":"\u7981\u9601\uff08\u7ec8\u6bb5\u573a\u666f\uff09","level_id":2267185542,"netversion_u16":[31079,49307,23536,0,0,25536],"placement_sha256":"61f321e171675b0386d487c71cefa5aabf6c16948eaaf4d805ecd753c094c2c6","resource_revision":"apk-v76318-421629","scene":"NightEnd","scene_provenance_sha256":"9a433f41cb7aec4bbd2105c10939baed7f725cb726ab239d98f6a3e6cdb04abd","scene_sha256":"591ff132e2dab9a297c6b4ed883dc8d071fbf3b514188aff820db8c3a1015572"},"2307461961":{"actor_sha256":"3f8e382e77cfe117e7a066ba5a975503c38cb01bafbbb3648c10d67fa327bf21","cache_sha256":"41650d5ba3a10af259766175a33c8b63deb76c5164bdcec4c6fca71f5783d4f8","label":"\u7981\u9601\u4e0a\u5c42","level_id":2307461961,"netversion_u16":[41630,49307,23536,0,0,25536],"placement_sha256":"67a7a61a65d6fa19bd2327866523b79fe3b9f83ab287ea3b604a81bd446d782b","resource_revision":"apk-v76318-421629","scene":"Night2","scene_provenance_sha256":"8e23739606ca52db7c48a66dfb62b2108f27e4894a17c5161b63fc719453aa94","scene_sha256":"30a3067c334f44a86bd914666962f7803861eab82c8690df8294d8ce933bd66b"},"2350532176":{"actor_sha256":"fd722ec3a047f5f890e739a07cba569b05bcf013d3b3c51c59e916d32ef56a64","cache_sha256":"b57da7c86e070882cfa7bca04473255438a81810d1b424c83ac9bf7cec53a464","label":"\u4ed9\u4e61","level_id":2350532176,"netversion_u16":[16774,49307,23536,0,0,25536],"placement_sha256":"0cc0e007fb67d5c84d41a0ee60c28df3e90dd9703ce0effe4a08a217a7183812","resource_revision":"apk-v76318-421629","scene":"Prairie_Village","scene_provenance_sha256":"7b58f2bc4e56b5c4c0544373f7ca73dc2dfa22f0c0bb5dcdf1c28d9a1bbefe34","scene_sha256":"18725f15ecbbfb7f879baf6321467f7d08a9ed5781f4ed37789cb98e809ce9ea"},"2358907137":{"actor_sha256":"8b0d4634373838b125a82f5ba080be1e932026e3e34d79ff902fd8dea7c0b8c7","cache_sha256":"e27f2033122c449d0cb070a8252717ddd04f9120ee52453b932a66aa58d7dcc2","label":"\u7981\u9601\u5e95\u5c42","level_id":2358907137,"netversion_u16":[19043,49307,23536,0,0,25536],"placement_sha256":"a361d53b02403cda4b7b60538068fe3a6156fc2381afbdf939a0926655bbfe39","resource_revision":"apk-v76318-421629","scene":"Night","scene_provenance_sha256":"680834da70ce234315c34b4c9326ba20b55d5309eb2493d26932eea57ff8ad0e","scene_sha256":"e8dfe4990e5b35440004ca1568e27b2394df4890fc2b2bd6407407e29dc957f6"},"2360310676":{"actor_sha256":"324421abcf1f37cb3493b3fc4591d7ed4f51940a21c836b0aac5501c15531fb9","cache_sha256":"082fc50120e02c70a99a35de4f6fa485532f6696bf319dde0fc652b832f4a768","label":"\u843d\u65e5\u7ade\u6280\u573a\uff08End\u573a\u666f\uff09","level_id":2360310676,"netversion_u16":[33428,49307,23536,0,0,25536],"placement_sha256":"138f87d4f2e49f4726a0b2c7924b5b6ddb1b1ab5e6685a102452eab5a7f4090a","resource_revision":"apk-v76318-421629","scene":"SunsetEnd","scene_provenance_sha256":"f64239dd3c5aed255fc8d153961903644d3cf3bd150c76e8c91bcf63b43cc6e6","scene_sha256":"831b03d7a897facbf88edf0f00ff5af55334c78cf6ca720fd005505e6547850f"},"2394719185":{"actor_sha256":"82834ce5c921c70ab3f790820121ace3377428d4b290f79beefb6f8d21414da0","cache_sha256":"96a9d50807b4b4e6580111b68ff351a18298aeff7c3730fe57f2005fef99fd38","label":"\u4e2d\u592e\u795e\u575b","level_id":2394719185,"netversion_u16":[63310,49307,23536,0,0,25536],"placement_sha256":"c08ff7d269857f5b410dc32005ed3db0b9e396415fd6a3cd283736ac9bacd604","resource_revision":"apk-v76318-421629","scene":"DayHubCave","scene_provenance_sha256":"bf180182bd8a13b077aa265f5d3d60254783d3af69ea10703bc55ed329861617","scene_sha256":"e39130a73b68fc3cac71078faf94952b114c6edd226c0a5b566a90992be1ba58"},"2418094228":{"actor_sha256":"4b14162579707de7c9ba8e3d6873aa7d174a68c3d46f06ac0d3162d3db9263e5","cache_sha256":"1d45658e293285efd0d6e43e0a75aec13ea2b5b590706f7aae15c27d78fe4ff4","label":"\u6076\u4f5c\u5267\u5bc6\u5ba4","level_id":2418094228,"netversion_u16":[10763,49307,23536,0,0,25536],"placement_sha256":"bb82fa77f21adf3d6097d351661046cefc70ba65d9a5ec02ef6e852298e75fcf","resource_revision":"apk-v76318-421629","scene":"Event_DaysOfMischief_Escape","scene_provenance_sha256":"346135b2785b3759a75dd98c7989798779b4da4f5d79e47c83cabd771e47eca1","scene_sha256":"2387df2aa42ded1a46c22864c5f80b3ee566115642d85ba2a6182af3034d508f"},"2458626594":{"actor_sha256":"a29c1c9e6f808b0f2bc3d9bf0cc19e28650948ad1d602f8679a349df46cd4a91","cache_sha256":"a49426725f649cf33ef777273893b7067bfd0cfae2a1fcb068951a5c3a024f7e","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u57ce\u5e02\u573a\u666f\uff09","level_id":2458626594,"netversion_u16":[17253,49307,23536,0,0,25536],"placement_sha256":"976905d97a9a13db0f0bc5220d33f5d0480f32d2e902de5091ce43aec4fcb40a","resource_revision":"apk-v76318-421629","scene":"Night_GO_City","scene_provenance_sha256":"d9475d0c998ca0fbf9c818fbb845d7a9427e458185a89f7248df75bdf9b51762","scene_sha256":"98a7ccdea0dbc4a976286adbcf0eb7f68e0fb834310b3d69a146bc60bc4d7a87"},"2477345666":{"actor_sha256":"e87dec89e8a1683a79f119bac035786b123d5b7b435ad49d8e5849109aab97ca","cache_sha256":"bf03ec6d2b965b37027b926580530726c8a9a82f25ef5ab555c876c0acaf30e8","label":"\u8774\u8776\u5e73\u539f","level_id":2477345666,"netversion_u16":[42205,49307,23536,0,0,25536],"placement_sha256":"7d62a79ce4ed6685442663a8f3c0b330be8266d7ca353c107d9350967788cf9b","resource_revision":"apk-v76318-421629","scene":"Prairie_ButterflyFields","scene_provenance_sha256":"71db4510ac8cd11248bd3d196859f2a9aa5b61761081505e84d8b200ac2214f2","scene_sha256":"68c3da5d98b20b0c7688b313927c3eb9e0d0a92f558c55560a7baeb1ef84131f"},"2518601":{"actor_sha256":"99916c60de406810e07c78e693a1e0726d73475c809c36af26b8cd3c510d7219","cache_sha256":"d247917955ceeabb873334ef79185d1b151b750095f20afbb5c40caeea0a367c","label":"\u6863\u6848\u9601","level_id":2518601,"netversion_u16":[36184,49307,23536,0,0,25536],"placement_sha256":"6bbca3fd972d013ec8a25e1a0744dd38e11a083b1b7e96642bcbfa199d9d6f2e","resource_revision":"apk-v76318-421629","scene":"NightArchive","scene_provenance_sha256":"df32e48f6d8e58614b708682ef23f92e010c83639c6df61097140de76ecf4b53","scene_sha256":"d6a7da1440f83ed70ffcee8599f2b2e35bdfd2effa49118789245186dabb3536"},"2611321369":{"actor_sha256":"b30a8eea1c52a4bd91d6264ef33655bd0e4cd16a4c5aaa6d0ffa7786ac638cff","cache_sha256":"50556e51d2f2bad994c0dc066f70a6911e7420133816e919c8792b49aa5e3032","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u519c\u573a\u573a\u666f\uff09","level_id":2611321369,"netversion_u16":[27680,49307,23536,0,0,25536],"placement_sha256":"fb7ea056ef10fc90a4856054113d07bede24803f4c3401091a56e1a5d6ab6ec3","resource_revision":"apk-v76318-421629","scene":"Night_GO_Farm","scene_provenance_sha256":"120f0ee7a81a8cd55e8dbfaff73e30a59bddbcbb59a9145d510c668081d40302","scene_sha256":"83b2d2c0df640201c605ed229b471aeadab527811594013db769709eb3145140"},"263580627":{"actor_sha256":"8d2b2fcbf022cd10fc992128f5ace206cfe4aaf3577f51117d5acd2cc53a2f02","cache_sha256":"002a7f9bf6c807550630e804a6bd1a12abd51e0a095629fb1f98e4dd991bd291","label":"\u843d\u65e5\u7ade\u6280\u573a\uff08Colosseum\u573a\u666f\uff09","level_id":263580627,"netversion_u16":[55802,49307,23536,0,0,25536],"placement_sha256":"cad0314e1678db859a045d7ba726b9d2ae4d40245744d5634c5fc8a4c3cfc437","resource_revision":"apk-v76318-421629","scene":"SunsetColosseum","scene_provenance_sha256":"ccf782e6f9fdc6d3899925282a24f82dab961b1ccd04a88b5b372423e9a54029","scene_sha256":"5de07440a98db33f095c99056469cd8b364ad102c6fa1efead2c8b648d5dad0d"},"2650921869":{"actor_sha256":"5eaa6355fdeb1dd5661c957e493db1834b980662a78128839225bddefee9a36d","cache_sha256":"851c50d4e8ceaa308bd18348e5e0a1b5c90c536f334f08a831f7081e410532d4","label":"\u9ed1\u6c34\u6e2f\u6e7e","level_id":2650921869,"netversion_u16":[22429,49307,23536,0,0,25536],"placement_sha256":"619b762ac28dd6af823e83760dc75112a8d142ac2f105967c73bc05c72e2b1f9","resource_revision":"apk-v76318-421629","scene":"Dusk_CrabField","scene_provenance_sha256":"e3f9659b6fa6f8a1f0d4f4d249919333504ba331456a278028d3d62a6d8f4e5e","scene_sha256":"9d2aee803420b2621c78c5a62967f6319a2e11423d4f444ddbb3adc5f16a5742"},"2720691892":{"actor_sha256":"ea40aab1ca698f00d2962c361db75327d2ef0a731f0370be50e3cfd7c1f27a1f","cache_sha256":"8e55bacacb9cd5eaac2b77a4f0b525a09bae920f23f30d09854a12ce21d7ebfe","label":"\u79d8\u5bc6\u82b1\u56ed","level_id":2720691892,"netversion_u16":[39931,49307,23536,0,0,25536],"placement_sha256":"852c4fa0b0e81b8ca9654bf8b3aa9543bfb6432a8ba4e01117ad9f50f5a52ee0","resource_revision":"apk-v76318-421629","scene":"RainShelter","scene_provenance_sha256":"edfef3e8a55643148ef0ebef400a9c05f800e890b766855719fb2dd3dd6dc401","scene_sha256":"fed3150a7bbce715be037b088335b0ad612db4d1ae76c9dd6b778bea18d84398"},"2779731386":{"actor_sha256":"05c7ac5865a52005f38d22c564aa111cb5ae75b7dedf490fbb7d201a4a341d28","cache_sha256":"bf97934ab87f816dcd65fa72b8b829fb8d045e44cc220103638dbc71ac13c888","label":"\u6b27\u82e5\u62c9\u00b7ExhaleInhale\u6b4c\u66f2\u573a\u666f","level_id":2779731386,"netversion_u16":[18128,49307,23536,0,0,25536],"placement_sha256":"fe436a61a68dfcc926a67638bc9d25d747ccae9b9cb5477ab8a08546222ce532","resource_revision":"apk-v76318-421629","scene":"Event_Arr_ExhaleInhale","scene_provenance_sha256":"241f596532615330f86a27a4f55a6cb597d69566bb9ec59e4d66b7896efc406b","scene_sha256":"eb06df36098d6537b00b79da9909b005b1dc2603343669644fcff8675835c680"},"2824771136":{"actor_sha256":"a57c2270cba0c323de6bc5eb3d71ce26c44b5c2729a178628054dec798ed5f35","cache_sha256":"1cfebe47606f80bf4ea11cc90a7bd60b76bfe69dd52212287cf43931766c6336","label":"\u5c0f\u738b\u5b50\u7684\u68a6","level_id":2824771136,"netversion_u16":[55942,49307,23536,0,0,25536],"placement_sha256":"810d7e7359755c2ba093c5c388d7a0042188a124430a581b153215363e6a9546","resource_revision":"apk-v76318-421629","scene":"NightDesert_Planets","scene_provenance_sha256":"63e4d8152a38d76a549660b701b229bb320c73a36e9b1657b05dfa6a0ce369b1","scene_sha256":"4cff87849e3942cf6cb2526bc57e8aece4f83693c97590811e9f1f5d25d017e7"},"2839585646":{"actor_sha256":"67b14e1941cfc364cd8c425fcd3ab3ca6fdddd945fe519f8634cf2ead2daeb34","cache_sha256":"cd7e58d72bc1319b2bff6ff5c9704de0d0d7f97a03423674a987c8197061ccac","label":"\u8367\u5149\u68ee\u6797","level_id":2839585646,"netversion_u16":[43870,49307,23536,0,0,25536],"placement_sha256":"d8d6d3746f5a2715e3d3ff7dcf7c92f589b87ae72bb822b1eba6085399a5af1e","resource_revision":"apk-v76318-421629","scene":"RainForest","scene_provenance_sha256":"6844a0257a56bd7beace257d26c5e275cfe6c6624afdc411e4979b99a0a9d04e","scene_sha256":"8514b4e5b44f338dc180aed82319163dd5beade0d549b15f44c3c34986fc2ea4"},"2899790755":{"actor_sha256":"f8468924acaf282ff0a0cb30f8ad4238bf3621035efd659772401f43384983c3","cache_sha256":"c6410f4b387f86793c859e5fcec0eb19558166e66baf93e6de4b5d9a0c900094","label":"\u5e87\u62a4\u6240","level_id":2899790755,"netversion_u16":[41436,49307,23536,0,0,25536],"placement_sha256":"87ab1c70cd47a06ebb7bbc3635eb558b2adca7a70947542912ba426013c8fe71","resource_revision":"apk-v76318-421629","scene":"Night_Shelter","scene_provenance_sha256":"0aa8af2611f8bdb6984b42af34973e2cc396d0db68b7791547cd878630c86f04","scene_sha256":"685aa745c8ebc9aa8b22300abaeb60599002e237b68d8909b34193bee9a2cf05"},"2920756220":{"actor_sha256":"6951844433daf3d89a90405bc66757d3bd497c362b3192ff3a7d58f2923cebf1","cache_sha256":"0f184e6518be456a9fa466bf320632043b81ffc671b2ce8503e4fc818bc9106b","label":"\u5bfb\u771f\u4e4b\u5730","level_id":2920756220,"netversion_u16":[33380,49307,23536,0,0,25536],"placement_sha256":"8744a11f4f4ead45bff087cfdaece03c58e20515e903fb73fe7c50d06b9eed84","resource_revision":"apk-v76318-421629","scene":"Event_Personality","scene_provenance_sha256":"5610fb28797dd1e0363e77467845047bfe56cda25d549a7708bd5fc651375e02","scene_sha256":"02e86c3d07d338cada7b4488f9c4f249b0293b688029c6bb88db1ade924d177a"},"295816905":{"actor_sha256":"c7c414d4d7e9ea5b90348e7749e119218072980010c3a8b9dbc89871ac961765","cache_sha256":"8bbc4a437621948385e2e883085e3fab119fa00dc5a093cd5939a9d05879d3dd","label":"\u79d8\u5bc6\u57fa\u5730","level_id":295816905,"netversion_u16":[42307,49307,23536,0,0,25536],"placement_sha256":"d02ac2eb92bb922cff99c1f4383124f10b7c496dd4795d8a708c2859679267d2","resource_revision":"apk-v76318-421629","scene":"TGCOffice","scene_provenance_sha256":"4dddecb98aee3c409115314c4581c510e4a2cafa83ce01ad64b5154ff675d046","scene_sha256":"853242c6faa7a866642a7d13566b18f556615d90dc474d4ded1c790e9131cc27"},"3051518728":{"actor_sha256":"86a4d28567da96f6d1b220e7d4db40662920250525104abaef9fa8ae6d6cad2e","cache_sha256":"f561d2ef04b83ebc3ae8d11b46d4a33b4794fbe018e50bfdf699a4de273c64af","label":"\u661f\u5149\u6c99\u6f20\uff08\u6d77\u6ee9\u573a\u666f\uff09","level_id":3051518728,"netversion_u16":[35732,49307,23536,0,0,25536],"placement_sha256":"94013f8508915dbc9f9c0dd1830d4468a52e7110bb2d6c300875edd81dc0654d","resource_revision":"apk-v76318-421629","scene":"NightDesert_Beach","scene_provenance_sha256":"10e8ebc2428e56d4e0c0a9753cc7fedb49374be5861a3d5f8de50e779ad3c81e","scene_sha256":"b6fb0ec69ed3422771b1988c868d621ebd205f985fb33e325114ac3e94d775ee"},"3057325709":{"actor_sha256":"eec3120667ac2cd3f946a76e410e8e4bc4ba1435a842fe5986d43444889f2ba7","cache_sha256":"9da97192e0dc2b192408e282993cb94699093499bc3c32ed1d9db2ce45ee8409","label":"\u5723\u5c9b","level_id":3057325709,"netversion_u16":[13967,49307,23536,0,0,25536],"placement_sha256":"8612a76c15134b9e9217bcc46513fe9f2671c1c3cf1a61c7e008927328946c01","resource_revision":"apk-v76318-421629","scene":"Prairie_Island","scene_provenance_sha256":"59dcad4c25737a1f77ef246074ac5eda25edcc5b3c1344c5df41fb375e3a000b","scene_sha256":"3b2d96efb5e6f38a2103b138c855e77a474c9af636442c91d2e980357b1ca8c3"},"3082753918":{"actor_sha256":"ea474e4683d84fb30bde97e0318495070411c5c72a300259ff8bc4a292ea71fb","cache_sha256":"804688f938325de222a4bb99dbfb6c0fe001a9b031e1d9e8f682959a5f4ac3b8","label":"\u8783\u87f9\u7684\u6076\u641e\u6d3e\u5bf9","level_id":3082753918,"netversion_u16":[21041,49307,23536,0,0,25536],"placement_sha256":"52e21b06e9a4d7f08c3bf5aa1ff64a034b9b6b9799346d0d0170f0f34b60ea6d","resource_revision":"apk-v76318-421629","scene":"Event_DaysOfMischief","scene_provenance_sha256":"6584f796d20fb5414fde86ae0ad842925bcff5d6e26c4c593221c7f8c3aaf55d","scene_sha256":"79ac46e0eb934de0d6d6f70d3f730c4b3107f678567ed66abc9688feeba9db9c"},"3092325157":{"actor_sha256":"204d208c5793dede7fc715c7c8a99d2bfff3bcba72b271fb9672c1c6b7926273","cache_sha256":"baa6a075fd7ea4bb12bf851430d6a5272911e2cf74f2ddc4a1f9d8c62307d61d","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u5c0f\u9547\u573a\u666f\uff09","level_id":3092325157,"netversion_u16":[38803,49307,23536,0,0,25536],"placement_sha256":"24100a50a1199742c7ded0650fb971c4d0a37a589884121a3dddf3c820488a51","resource_revision":"apk-v76318-421629","scene":"Night_GO_Town","scene_provenance_sha256":"fac9179d6733d279c1a9124ed1c905ce106ee3f09fb24b62f88b24051abae4ed","scene_sha256":"9d10ddfac75c893b1c547ea3544d8ca2a771c7dee27f961b9b7450f573ad9254"},"3110721718":{"actor_sha256":"ceb0f622f71bcbfe81a00a63a45485640b768b822ef89db2f6a5098181bf4a35","cache_sha256":"8ec195a1366f8a27d8ec3f484d86afa7ecc45481ce2ddfe7e7899ebe75087af9","label":"\u4f0a\u7538\u5927\u95e8","level_id":3110721718,"netversion_u16":[36413,49307,23536,0,0,25536],"placement_sha256":"08f479e5d19afb5ab1265ffda5ca6a378962945ef43f930ab522fd4a0bc2d66b","resource_revision":"apk-v76318-421629","scene":"StormStart","scene_provenance_sha256":"47eb80aea1fa93c104bdb052808fb250f7ba56f40c4f33226ca1286646dbb212","scene_sha256":"99da084cfbe613a5a36c1bdab050d17ec283808391e94e9f8116917b26e8be6d"},"312004957":{"actor_sha256":"b7b5c8e2c6047a778e23840e230c8d6f9d8c5ce120642d1d962bb6889bb394b9","cache_sha256":"00faa93b1e771499ebc2ba7f2a50283602f4e8e79fe044c2c4f53908b67be941","label":"\u4e91\u9876\u6d6e\u77f3","level_id":312004957,"netversion_u16":[57895,49307,23536,0,0,25536],"placement_sha256":"96625831f34b0ef7a6f9a1ccd91a637a2d0406dd8496025f321a2585acce6aa0","resource_revision":"apk-v76318-421629","scene":"Prairie_NestAndKeeper","scene_provenance_sha256":"45ad76a513835015649154290e7b1a71d81b317647ae702f6174b50c27c4fd98","scene_sha256":"86bbb13c7ae5c2de291f1ae9e70a7b357ae6b03a0268820c626061621ce82d68"},"3203226771":{"actor_sha256":"94c3ecaf67985277035276691888c3977e1f50c3c5ff836c45ed0306e3d3daed","cache_sha256":"9aed57d0373c97b156defb4a6d19e9cb64717cdebba54edc31f76513bf19c7de","label":"\u7ec7\u5149\u9601","level_id":3203226771,"netversion_u16":[34241,49307,23536,0,0,25536],"placement_sha256":"1ea7582a65ae4936ed00ebff252c1d70ef90589635537f93fb6a0a3fa29c223d","resource_revision":"apk-v76318-421629","scene":"Night_Workshop","scene_provenance_sha256":"2a22faa465c40f3e96ac241a411d048260d1eb5517c9190b3ab7b588cca5df9a","scene_sha256":"3b09d8df361b6e7cbf5df94991c1f6592a852414864a1fa5f9625dc3319fcd19"},"3244931597":{"actor_sha256":"29affd9b7e48d33b8132270bddb53fd28c1bee2153da42ac1ba2ea364691b9b4","cache_sha256":"1e738c197bcaaeb0d9f693441c1847c207f4dc4dccf1c933754379ec251ad87f","label":"\u5e7d\u5149\u5c71\u6d1e","level_id":3244931597,"netversion_u16":[4656,49307,23536,0,0,25536],"placement_sha256":"9902763038bb8ef56d9c82b5b4f5fe7e3a5f6ffd5cf87d36c9268f97620013b0","resource_revision":"apk-v76318-421629","scene":"Prairie_Cave","scene_provenance_sha256":"de2643898beb8b2608377c2f8bc24840f085d5bbea5302df2ec299ddd90e7747","scene_sha256":"11592a52f0b2b93d37d07082d84c1e20b2aefd69d9ca1062b7d8c305094e4262"},"3258961934":{"actor_sha256":"4fbaff42b0cc0c9076bd94c87bce4016c47e5881853b0f40adc80fd8861f4873","cache_sha256":"e67558ae13b5c7bcb3b29fbd6ed735d90e821e87b2141cc1a721c9559a7ff846","label":"\u661f\u5149\u6c99\u6f20\uff08\u7f50\u5b50\u6d1e\u573a\u666f\uff09","level_id":3258961934,"netversion_u16":[2938,49307,23536,0,0,25536],"placement_sha256":"0f07baa344b67989790dc4ced605a0e1df26558e520eee81e8e1cf64cf702582","resource_revision":"apk-v76318-421629","scene":"Night_JarCave","scene_provenance_sha256":"e05171612387af5a09da54454a02947ec095943464f2848d84afacb544851707","scene_sha256":"866a3bb442b0ec310a99e1eb85e371633914acc59aec2186435f589902537583"},"3317260872":{"actor_sha256":"4c21970b39b03cc8711e139b1cbb96cb688ba275881a2ee28e164be0ae90455a","cache_sha256":"8b51bb12e2844d92a7c3c6ec7bd175ad57252275f9bed0d325b23b0aaaab2eac","label":"\u5927\u6811\u5c4b","level_id":3317260872,"netversion_u16":[8826,49307,23536,0,0,25536],"placement_sha256":"1f4e1d891c4a32e3ee73f90957e9b5f7c92284ccf3aa511726018632b3b69ecc","resource_revision":"apk-v76318-421629","scene":"Rain_BaseCamp","scene_provenance_sha256":"7eb596c4ac199d5d442c72bda009d14f98bf3df75c5e1ad108e164e344b74560","scene_sha256":"82722e0795cbe7b144095954e39409b136ceae142e861ca05cf7e4b3c61795b5"},"3368878998":{"actor_sha256":"02f568eebb48be293120c1cfd2c1bda9c62241463dfe0ac99a083ad59659e6d4","cache_sha256":"cf31f1b5fc488220ca95e1a07cad9e4e0d90e8c3100fbbabc9b3d4ccff81d18d","label":"\u51a5\u60f3\u4e4b\u5730","level_id":3368878998,"netversion_u16":[34484,49307,23536,0,0,25536],"placement_sha256":"f221590b6000b0273c8726e57f6914359ecbb87aee98482fdd53499a2ffef07c","resource_revision":"apk-v76318-421629","scene":"MainStreet_Soundbath","scene_provenance_sha256":"03c331cca88b54a8f31962cc5a33ae73f2444a0bf2f6f093fc96b4325bbd8538","scene_sha256":"96029dbb28ca9fbaa270ef58fbfae38a2320797d1fda5f28481abfecb086962c"},"3429122502":{"actor_sha256":"5bf3408da5ac8665b7875ce16b8483024e378df0894038a188c026f866a553d5","cache_sha256":"6c6128f6c349f79cc50e911b81dbb65b34226fc19a206ddc2d18b0ab22479141","label":"\u4e91\u5cf0","level_id":3429122502,"netversion_u16":[30884,49307,23536,0,0,25536],"placement_sha256":"1a97b752589733126b6494a3f4fca1b0b41aec7666f1717da39cb40ba2aa07d5","resource_revision":"apk-v76318-421629","scene":"Prairie_WildLifePark","scene_provenance_sha256":"40dd59cebe0f866877bd36b74f95d8ed10da10853855ca4827aa682058803c58","scene_sha256":"a84f0231fcdf96fb0e331f4f39986f4ee7a376c2db44537502eef9d29f2d212d"},"3437135515":{"actor_sha256":"54a844bc3c549f3665533ef37b0822b6369fd0ddfbec91a60e965e164b9791dd","cache_sha256":"03deb6a697ddb2adac4b0d90ab36d1ba6998e15d46ec641a91c426a469593d93","label":"\u91cd\u751f\u4e4b\u8def\uff08\u4e2d\u6bb5\u573a\u666f\uff09","level_id":3437135515,"netversion_u16":[15789,49307,23536,0,0,25536],"placement_sha256":"f82427ad839c942a8400da71782c26c75e463671f03653371860cb55b32d4264","resource_revision":"apk-v76318-421629","scene":"OrbitMid","scene_provenance_sha256":"8888c4cfbe0fb45e923ce50f0ee9a4e392badba582c891a92b056e7798e2e6dc","scene_sha256":"711b8d3578aa99f4f0e7b1409e250b097d8f32af9a6077b1052f8b404f335826"},"3452301396":{"actor_sha256":"75b8b7ee4697d81ce3348e7fb8b18b497b109bea8143b0bfa5a516860c36349a","cache_sha256":"9fa87fb1985e1ce469aa5c4d53148cb0146ffb35e2a6c21e58efd0926497fbf6","label":"\u6708\u5706\u4e4b\u5730","level_id":3452301396,"netversion_u16":[64024,49307,23536,0,0,25536],"placement_sha256":"25090b9c82a0be0dadd8367d3c8fa5b7ee70878564cc94e9d1bf4b0514e67118","resource_revision":"apk-v76318-421629","scene":"Event_DaysOfMoonlight_HugeMoon","scene_provenance_sha256":"c8a039d0cb3cc9519296bdbf743c9e6e1f644985f4cd09f2b331bac76a11f539","scene_sha256":"53d633f17fa614287a330cfbd65e595a1230f188b3793539edf5f6886d8d985f"},"3463762797":{"actor_sha256":"105027bc22f6d9f976595a9dd861a10d3ff35aa85153c91cb5f1d12d71550653","cache_sha256":"f06dfcec8286c4bc75e8e60ef7e607f394c8c45a4c6fd7266e6abf077a0b3261","label":"AR\u5367\u5ba4\uff08\u5185\u90e8\u573a\u666f\uff09","level_id":3463762797,"netversion_u16":[35344,49307,23536,0,0,25536],"placement_sha256":"2dc059340ff8c6116ea738bc924b9a6c3862f589a662544107b38eedfa38fa12","resource_revision":"apk-v76318-421629","scene":"AR_Bedroom","scene_provenance_sha256":"d1048909d5e9959db8eeaecb05de0e1395ca9232aed5b577a692c7ffcdb1f30d","scene_sha256":"8d26d3aa587bc6608e1c35ad62143901e7a2b8857ef0de19ee8fdbd86b8149a5"},"3479786579":{"actor_sha256":"64746aa292ed96bd935bc679123a3fcf9cf5ca62c5b61811e1a0d65ddc4d3dc2","cache_sha256":"ec4bac529ed1afe672918a1ee88cf9f5bede6da5daddbc4c27d52221d13bd734","label":"\u4f0a\u7538\u4e4b\u773c","level_id":3479786579,"netversion_u16":[10808,49307,23536,0,0,25536],"placement_sha256":"e35e5f4b34d128b2ba54da3716d3e06bc5e01bd2fc8950c0ef5868de7b2d4868","resource_revision":"apk-v76318-421629","scene":"StormEnd","scene_provenance_sha256":"4d33acacda21ac7f11d65fb0e7ea4dc90ab298e1c4bf77ce02415c6ea8a3070a","scene_sha256":"c8f915fad61cabc21d9feb258424f1ea139847a07dacf067f78b9791513bd1b2"},"3493360294":{"actor_sha256":"fff84917324b2788580b17eacd5a801ee421065390148807ddc0a60e51d3bc0b","cache_sha256":"5f9e4bcc5c200b07877b0037516dfc7c85872ad82d47ab43276fbe1af04b143a","label":"\u57fa\u5730\u8ff7\u5bab","level_id":3493360294,"netversion_u16":[10709,49307,23536,0,0,25536],"placement_sha256":"323d65c4040b7804521e062e47f9b3760cc2c46a1565377523e281c466e57785","resource_revision":"apk-v76318-421629","scene":"UndergroundOffice","scene_provenance_sha256":"62ef92321fae1f3b6d7a98d29ed5db4445839a51c782c6dcf263c2741026fac8","scene_sha256":"87386e39ee8e1e07a76e87af338c4b9cc1f7623b7b484abdaba0539e867a3000"},"3516048122":{"actor_sha256":"f1bae32c5aa7339832313d07dc72857355f922b384e55115c77a1c027822703e","cache_sha256":"d8eccfa873e9f548a33ca032f026b2374a577a5535ccb537811c419702055b87","label":"\u85cf\u5b9d\u5c9b\u7901\uff08\u7ec8\u6bb5\u573a\u666f\uff09","level_id":3516048122,"netversion_u16":[386,49307,23536,0,0,25536],"placement_sha256":"93820df7163fc9eadf7882b18ad15981db338bf533e14de81a9d34f747b38d7a","resource_revision":"apk-v76318-421629","scene":"Dusk_TriangleEnd","scene_provenance_sha256":"b8545e386e4cdaaa5b6711537a4aa171312d9f82b6733256fb9055042436c2ab","scene_sha256":"bd1343e39edaf601952754905f18001a403a952af070877fbfec95f83fe8c146"},"3526133726":{"actor_sha256":"f766e8014cf69300c552145c9e48336311b1c85c877d76e2f91c74ff85da0eaf","cache_sha256":"5ab46e4665fe1b801f560caa86e41fe8744d9c5f7ce8e09997bd5217d270eb89","label":"\u9047\u5883","level_id":3526133726,"netversion_u16":[63789,49307,23536,0,0,25536],"placement_sha256":"dfb5802d71d1f7607cecf3bce2b69c83104ec3aefe91dfeef7429bd565311d69","resource_revision":"apk-v76318-421629","scene":"CandleSpace","scene_provenance_sha256":"3f4862cad2aba0bc16b149821d693c1527ef3a78dc03dae228b923d6f4f1c9f4","scene_sha256":"4b2c1df17afbf9f73ef8a6ca13a117bf01850d55b9d3ea103019b9d6ccbcf7fc"},"3530028462":{"actor_sha256":"d7390c30b52231624b196f28fea75dc2a152841800cb6c9a572b54e76a175564","cache_sha256":"8ffb313169e0ef233b5a21ba5e83e68ae45db4702c58917e95c0b4183acf9a17","label":"\u6b27\u82e5\u62c9\u00b7Warrior\u6b4c\u66f2\u573a\u666f","level_id":3530028462,"netversion_u16":[55898,49307,23536,0,0,25536],"placement_sha256":"93a0d6769b192add911dc290f9f24209e667f9ee33c27e43556b9e48eb5dd32d","resource_revision":"apk-v76318-421629","scene":"Event_Arr_Warrior","scene_provenance_sha256":"9434f9592e139580556a081759e5610025a23bf4f8dc419404b13be43ae7ff6b","scene_sha256":"1cbd11f5c1c88ebceb23312f00e18033f9cc965a5ffadb136d159433d059d840"},"3530471012":{"actor_sha256":"d7fcbf50e0d63dafc37f81d4d8a0417b150fcdb3fb08ede578dbf78b97775dc6","cache_sha256":"fae8e93bbc4b8d8ce78ff7d0a4d0739238fd8e00142d6a882c108b6b58ba88d4","label":"\u8eb2\u732b\u732b\u6c34\u6676\u7403","level_id":3530471012,"netversion_u16":[3722,49307,23536,0,0,25536],"placement_sha256":"dc409dca28945a7dbfe77c6bb0c2de0b4cbac8a2ff01252d594ed52132df81c9","resource_revision":"apk-v76318-421629","scene":"Event_HidenSeek_SnowVillage","scene_provenance_sha256":"89c15936cabafbf52d910cca9f5e72285bcf70d368f41fc0e440f531059551b2","scene_sha256":"43a88f1afac32b07cf15e3d980001407dee77f99a16e58b3239a4646338dd3dd"},"3667302771":{"actor_sha256":"c92e91dfbd7887b7fa4fbb0904a52291d66d6d1e570d935c98d09e23072f4744","cache_sha256":"9b435424e593aa3d6cf00d31acb3b1131dd7a761196e1acabfc26de5462c0b06","label":"\u865a\u7a7a","level_id":3667302771,"netversion_u16":[49811,49307,23536,0,0,25536],"placement_sha256":"a2479253b60a25d0236175955a423978495b9cec94a9d6d02c0f7386ea1bd9b9","resource_revision":"apk-v76318-421629","scene":"Season24Void1","scene_provenance_sha256":"a0d9191342d3ebbe6f61e6a7a38e3e130031d91d6e2d9fa1718280dba603b23c","scene_sha256":"6baaea3e11a20316eed5709b49ccdcc80dec95d51201d5f89689c00dea176e0e"},"371156238":{"actor_sha256":"e72a3bf0bc32ee29e634991fea909468591df69b522649ded6a9c1a52f3151a1","cache_sha256":"3d2c4eb253247c948acbddf80ddd4b5abf2812ebf9ad333fdd080d1c6b6ed6e5","label":"\u66ae\u571f\u795e\u6bbf\uff08\u8fc7\u53bb\u573a\u666f\uff09","level_id":371156238,"netversion_u16":[591,49307,23536,0,0,25536],"placement_sha256":"6bdc2f0d2ecf93d1259aada7a02913fedceab7af926a7a98054149295278714e","resource_revision":"apk-v76318-421629","scene":"DuskEnd_Past","scene_provenance_sha256":"0ea3708c3f07f276772d0280e9a66e9dafd9a44a8d52bfe2e37f59f65b6e579a","scene_sha256":"13f383793da4396427d83c8aa23238ae0709d141eee6890eb924bfc93f65c81e"},"372393162":{"actor_sha256":"814cfd096709a689237907bfb1e0732ac41ef377976adadc467afcb5217ee118","cache_sha256":"80620318dc44af824212a64ca931f961dd69b31555f9eb770880b55c9213ce02","label":"\u7b51\u5de2\u5de5\u574a","level_id":372393162,"netversion_u16":[31779,49307,23536,0,0,25536],"placement_sha256":"565ac1bea35aa5bd4c8ae135d7224ad56f9305b5da518309a904f4fda0129446","resource_revision":"apk-v76318-421629","scene":"MainStreet_ShopProps","scene_provenance_sha256":"c8505bf7c5035738d50da630681f23e210244535f5403d4e87026312ce6bfeab","scene_sha256":"e9759d17046805da583953e30aebcab66b217c42927e4d6b46ae732366b26905"},"3730271824":{"actor_sha256":"1f1e28cc825f2ada52e52a4837ffa0d329442f891675d75bc8022f3994c3f78c","cache_sha256":"1343b91e0c171ae37d28e75369cf2e8b11baff1a0b3b6ce30a8203ae0b5e7489","label":"\u9752\u9e1f\u5267\u573a","level_id":3730271824,"netversion_u16":[667,49307,23536,0,0,25536],"placement_sha256":"80f639bc27f615f62ef09d146665cb0b769dcc7e40e0383b82fa5ad0dc83c58e","resource_revision":"apk-v76318-421629","scene":"Rain_BlueBirdTheater","scene_provenance_sha256":"344fd26120873a0f3775e08945e487978cca8ee44496524d59f4ac02f16319b4","scene_sha256":"c40f1f81e9d9f353860a4a255244a90b729b68651c16bd40810077a13c4c2dbe"},"3743646255":{"actor_sha256":"49867f593448c4efb5f658e4e186e5b9e13301c7d6391e6b414c88badc004a6c","cache_sha256":"9c0a3306f2a57c8e7cb1766ba9433e1d251925378a14f6bb96eddf8f81235549","label":"\u72c2\u6b22\u8239\u961f","level_id":3743646255,"netversion_u16":[59928,49307,23536,0,0,25536],"placement_sha256":"107d80c521b39a12c2b04957a62b694a9e22eae9fb8159b604b4ce3614afe581","resource_revision":"apk-v76318-421629","scene":"MainStreet_FunArmada","scene_provenance_sha256":"0a552f29ee469a693645a40831d52110d089020c03dd750572e08397618b6ec2","scene_sha256":"12aa9e3514968622ba6b082a6a4ccb60d63a4bad3fe1d7cb70e3f2803ac1331a"},"3803142938":{"actor_sha256":"042b45bbf4d05b426a7c5124286f0da499be507ebb3915493fdb2b0b37ebc48e","cache_sha256":"a972b242c97d54f64bfcc0bb214f363b03b1913bff949a75941c87ab9aedd6e3","label":"\u59c6\u660e\u6545\u4e8b\u4e66","level_id":3803142938,"netversion_u16":[50914,49307,23536,0,0,25536],"placement_sha256":"69307e4cc659373fdcc7932a9f5a521703aed97346d1f57d62f4139853f1a141","resource_revision":"apk-v76318-421629","scene":"Night_StoryBook","scene_provenance_sha256":"82cb03ba15de5fed31be60058515fabee98a9c00aaa362c1481d141fd020ed1e","scene_sha256":"6cbdc79ff30c4a91d40eba76f8bbfe706834446fb89cbf01d52b07696ce36597"},"3884142720":{"actor_sha256":"e2544c489ebddd2336012d086660cf776e3f40c63c8d97657a136f5a4c719f30","cache_sha256":"94a1973c130cfa1042a234b9e5ea44ea1402b189e9747c3c1a5cff9f45c4c2fc","label":"\u6c34\u4e4b\u8bd5\u70bc","level_id":3884142720,"netversion_u16":[50622,49307,23536,0,0,25536],"placement_sha256":"883fb461bce5555c109a084d859f4acfc3b67ccb3e83e1bb3e2d6ec18f5a1a9e","resource_revision":"apk-v76318-421629","scene":"Dawn_TrialsWater","scene_provenance_sha256":"7f9ae4a393779b4e1173ea71116ba16b9f4f4f23994224113dbc6fb9db5e0171","scene_sha256":"c3528a294abfeccd16d1e5c693dab83d983a1e458a4e608c89febb29dc536159"},"3905084854":{"actor_sha256":"73a93b07388549fef47580c9d47589d015c3ea29a4898e30638981c6f3fc5dbb","cache_sha256":"ec353507c941fe35cbbddb5c6b03aa7bef13652c7118ed513bf4ecf041cefbda","label":"\u5c0f\u9547\u516c\u5bd3","level_id":3905084854,"netversion_u16":[15232,49307,23536,0,0,25536],"placement_sha256":"ec81a12dfc82dd42293a04f604ea167a8a6d562957a63865ba0270bf827e3951","resource_revision":"apk-v76318-421629","scene":"MainStreet_Apartment","scene_provenance_sha256":"796ea193f121cf4883c05c48596eab418b6129caaaafd9cf48e5291b88a43c85","scene_sha256":"4c56f65b36741b0526b17af9b0d5aa9998fb59f8e17fe6a4767e0fea08f83b05"},"3928396665":{"actor_sha256":"96eb7a7a73d4baf0d8f2c31b8d550c601f3fa9e268def621eeb273d727b849a4","cache_sha256":"825dffb69d5445eebfb0e7bf6f3b72aca7f78f5a392196a31d5471f888936b99","label":"\u8fdc\u53e4\u56de\u5fc6","level_id":3928396665,"netversion_u16":[64389,49307,23536,0,0,25536],"placement_sha256":"1817a372c09bb6bb5a082f1b8a93bf79132686c7b4c4b4917aae0ca82f4681ae","resource_revision":"apk-v76318-421629","scene":"StormEvent_VoidSpace","scene_provenance_sha256":"8caa28c7145c879107dd5b3d225cf9934a2ab694b15286e39bf2604cd77ad2a6","scene_sha256":"0e9d9750496091c89c12918a8cf566c1cda7df160e8ec74b5e27f362ef87b991"},"4011385311":{"actor_sha256":"69bfeb616ff928df0a2e45508eae2262724d7f941abc152ce95772246e3914ad","cache_sha256":"9e6e97ca0c4d59689303949deb66430784a3150c45c04044c7fa60481048034a","label":"\u6b27\u82e5\u62c9\u00b7Runaway\u6b4c\u66f2\u573a\u666f","level_id":4011385311,"netversion_u16":[58451,49307,23536,0,0,25536],"placement_sha256":"17dde896a2084d8d74cebfff5b52bd22c1d9ce731c0f95698942e4f5ac2ab377","resource_revision":"apk-v76318-421629","scene":"Event_Arr_Runaway","scene_provenance_sha256":"c7e80056f288fca5225e6ed8e183900e3f11eea8e6aa5cba46e736713eefb6cd","scene_sha256":"3fb44617a858afae1127983c83c536f4fcc9715ec823aaab7dbc8cdc9c79b456"},"4023907509":{"actor_sha256":"3ddb919af9c734a367652788de7720ea20e7ceba5a36a445a0ea73dc66a46e40","cache_sha256":"f12d1032bc0a498e01b49876a92f79a439c4c492cf883816054398eb9b41ae68","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u6811\u573a\u666f\uff09","level_id":4023907509,"netversion_u16":[4466,49307,23536,0,0,25536],"placement_sha256":"bbde77801976222d79b67d47a29b6b468682386f8ae0e3d7eb7b441edc4d9af2","resource_revision":"apk-v76318-421629","scene":"Night_GO_Tree","scene_provenance_sha256":"cac71674ed1bd345ebef89da39082da8bdd5e1495d8e3eee34b6cbffff15d30b","scene_sha256":"2492d0444916bc0998c97d2567ad62724a1c746f1c6d746ba33bf8eb4bcba015"},"4044469303":{"actor_sha256":"3a0bb38ece9da0aa19dfffa1745802aa53a9ab411312e64b80be6063902cd06f","cache_sha256":"de3e74b9116153e5771f098da5da90dcbbe4d5199e5ab36d45dea974c9149673","label":"\u9b54\u6cd5\u5546\u5e97","level_id":4044469303,"netversion_u16":[32106,49307,23536,0,0,25536],"placement_sha256":"e0973f2ca260a63beea9747856399ad141779c1f5c2fee4491b35f1b190ad44b","resource_revision":"apk-v76318-421629","scene":"MainStreet_ShopSpells","scene_provenance_sha256":"8b49af44013aab85faf22c1c9b09b6bd461ffe2372fa7acc6053302f1f123189","scene_sha256":"d3bcd110137a13c6fc0248e9bc613488ed66956c3dd033e08f802a51c5c5ee85"},"4070111637":{"actor_sha256":"aa5f0e224bc99fc918e6c7c3e042858720484213f157ca6769e697f70e433538","cache_sha256":"5ad81f948971fa031bb4c50657b327b5c8b11af3eca5e1e12370fcc94acdf93b","label":"\u85cf\u5b9d\u5c9b\u7901\uff08\u4e3b\u573a\u666f\uff09","level_id":4070111637,"netversion_u16":[2032,49307,23536,0,0,25536],"placement_sha256":"63f6545612816e8b9430bd3a14882cd16d4a70ca8ce777b11124cf50407127b3","resource_revision":"apk-v76318-421629","scene":"Dusk_Triangle","scene_provenance_sha256":"7f3ba230d28036a8a56958dea18cef9e29afe963af80b51e9d7c401ff12a543b","scene_sha256":"023c7c49fb5332b8df18b3a2da372642ef847f91a95624956242ce3613a7984b"},"4133595729":{"actor_sha256":"b88ce51dd767e5cf822259d5dbe8485e76a1ca588b9cdd153d09c40408666d7b","cache_sha256":"5fccca4210128e29d87fd54895446588cc867f004469df57996ec1e8b7fafa5e","label":"\u5730\u4e0b\u6eb6\u6d1e","level_id":4133595729,"netversion_u16":[35163,49307,23536,0,0,25536],"placement_sha256":"07491273d4327fda4f36df7bc3226a86683b1815fcb0cc2279a3cca978f9df3b","resource_revision":"apk-v76318-421629","scene":"Rain_Cave","scene_provenance_sha256":"77d21879aed4431c68519f15085ec8f8a288ad8916baf598ab7f6ac0e59543fd","scene_sha256":"440e2a994b02251c9113798a3753b70d94395542ecfb3908779b5843680986a3"},"4158956653":{"actor_sha256":"8f25e35eabc04e03d19b5c453e7466ba4b18564625707fe1a7275debcd06817b","cache_sha256":"98c2edf344e7aa13376c223607382bd54a5d703dcc6ded8a6344d71da1a541c4","label":"\u66ae\u571f\u795e\u6bbf\uff08\u4e3b\u573a\u666f\uff09","level_id":4158956653,"netversion_u16":[54113,49307,23536,0,0,25536],"placement_sha256":"874e5b654e862fef7f775f22256d6337eb07019c71dc5b3662d19ec0860f0bc8","resource_revision":"apk-v76318-421629","scene":"DuskEnd","scene_provenance_sha256":"cfb25b0a752f09e48d23647ccfc1b48ada06864a42934ef1c6843b458751c7c2","scene_sha256":"a0d86981ef2605a101e201b36668565c01dea2c2b30dcdd012bbb3f8dd8f09ac"},"417578099":{"actor_sha256":"abdbdedb8f537d31c3867d0410bc9ea0227fa0d95216bec408b309f7d8dce025","cache_sha256":"171b687178f7b9c5b17d5803ebbf0e810d3395cfcf1df271e5fe74934d0b267a","label":"\u7fa4\u661f\u5f71\u9662","level_id":417578099,"netversion_u16":[14292,49307,23536,0,0,25536],"placement_sha256":"489c64a32ac2fe14f2acfcbad709718c5d64561eb8e042cb886d0d3e756084bb","resource_revision":"apk-v76318-421629","scene":"Event_Cinema","scene_provenance_sha256":"b68c714d0a3858c6e1ce97f89877c4c5b847b07ac754ff904028dc16cc77d0a9","scene_sha256":"b52069ad48c8acdcce769a0a3bd275ca1ad4ba5f2c38b541d21acfd02bbaf06d"},"4182564783":{"actor_sha256":"000b5dd97519ee462f85e05a5fa6710944f84fe6c15053374da864e7d95e8151","cache_sha256":"6b7f87d43e4e0083930e092f47dc738c60db3ecd15cdc818261881f7de88fe2f","label":"\u6b8b\u706f\u589f","level_id":4182564783,"netversion_u16":[19522,49307,23536,0,0,25536],"placement_sha256":"10146edbc2c4b1971e2dc62ee8accbacf58ec79ae45cfd990266acbf9cf6469c","resource_revision":"apk-v76318-421629","scene":"Night_Jumble","scene_provenance_sha256":"7eb4d781f1e458440003da1434992ba31151e25443512405fa05d8ab5c500187","scene_sha256":"053e19c97ba1eb99e7015f5cb5b713bb74973f0a78ad16679c90e51f3decbd70"},"47024056":{"actor_sha256":"8ccbe4c1aa68e03870f2f2781db049c010d9210e4852e1756e15a22164480f0d","cache_sha256":"ff2d8b9af29278a6f254d353f915cef3ce1f7361f098c77c90e73c096596d1de","label":"\u6b27\u82e5\u62c9\u00b7EyesOfAChild\u6b4c\u66f2\u573a\u666f","level_id":47024056,"netversion_u16":[47197,49307,23536,0,0,25536],"placement_sha256":"22baee7135ced6c40800f80fc01295890aa0e76ee5a46263d8c8d65456b1edeb","resource_revision":"apk-v76318-421629","scene":"Event_Arr_EyesOfAChild","scene_provenance_sha256":"f537898502f4aa378c355707ae00c38aa97b9885b6c17c3aeef10508cfce9a5c","scene_sha256":"9515be9f47b4a6d61ba747db9af1e37735af70c370a5d4533825aa92bd8cff7c"},"507487826":{"actor_sha256":"17d02195417279556d034d571ed847fcfd57e4d127600857524f81edb52c49c0","cache_sha256":"5c529f9aed30124564aaee5e034b2351762c754dbbf19082309243faf0871d97","label":"\u971e\u8c37\u795e\u6bbf","level_id":507487826,"netversion_u16":[17841,49307,23536,0,0,25536],"placement_sha256":"309faa64ac5725486bec41f5fdaf6567faf0f93eb7ff9f496843e84072501b25","resource_revision":"apk-v76318-421629","scene":"SunsetEnd2","scene_provenance_sha256":"b9f31bd5dc025463d11a82d7b50e84c619c8302154637daf03c9bc317c5aae6e","scene_sha256":"515e588460a90c80ff7d805bef9a7543370ac99ef66a35457eadc7fb0785d58f"},"530831471":{"actor_sha256":"f805d4982d94d86af3a6c34c5be86d3f46c36265f98827ca7655692ada490d71","cache_sha256":"c26e24eaae3fbf63125e8e61989b29540fc00bfa8f063e4d883542d08bf91f99","label":"\u85cf\u661f\u9601","level_id":530831471,"netversion_u16":[29786,49307,23536,0,0,25536],"placement_sha256":"a74e7e8e7f0b039f75d873f962c78a277703fd6d2fe04b0473d58c440bc393eb","resource_revision":"apk-v76318-421629","scene":"Night_IPHallway","scene_provenance_sha256":"031e636d1cf97372cedb4f80bdb2327638fad3f7f781f2835b6644d72c90e110","scene_sha256":"a3d83ef04a7c4d20b2501fa046530ff8e84790a9dc51cabf81d78c2f8a21336d"},"567986524":{"actor_sha256":"89dfbe640d00dc101fea260bcce57add869d0ab79358bee70a00c352a597d031","cache_sha256":"857ba3e351430f312a7a7b8d79ba24be0a32cd6933152e35eda8396e50eeff24","label":"\u91cd\u751f\u4e4b\u8def\uff08\u7ec8\u6bb5\u573a\u666f\uff09","level_id":567986524,"netversion_u16":[28077,49307,23536,0,0,25536],"placement_sha256":"4c78cd6624cb48681eef4e4c5d722d67d1b26eddd59ea075499457cb69a74a5f","resource_revision":"apk-v76318-421629","scene":"OrbitEnd","scene_provenance_sha256":"802e26cb22e566dc0541756e9f27cc82d91f605a1d4c0075bbd5d94550a56c99","scene_sha256":"d55366e2475f77bbdcd8058b7c0c5cb68c0d7b38dff70f2ea983cc30572cdf0d"},"571720490":{"actor_sha256":"c77d6997e0831e6a0f18f294afa70b1854d70179edc3ec98d9343b859e2b8c8c","cache_sha256":"8d2a79e675f27e6a781dce83d120c1c0accd1fbaf435d517761cec2df10b7e6e","label":"\u6ed1\u96ea\u8d5b\u9053","level_id":571720490,"netversion_u16":[41011,49307,23536,0,0,25536],"placement_sha256":"bf8225a79b6b60c36c013d4476bd781e9c7876c789c29ed655e37aa1ca1d0576","resource_revision":"apk-v76318-421629","scene":"SunsetRace","scene_provenance_sha256":"9f633358bdf0452ad0351a13fc5b3974a1548b65e9bc8a331342811d7dc10a45","scene_sha256":"706f371644165d9ccc35e3b46189169bd1061524878bbe7866dd95781d7ebfa9"},"607516458":{"actor_sha256":"bec1f74076d15c0bcfb5bc05af675f7cc58ab00ada49a7ec3f91d4df38c11648","cache_sha256":"d595a457ea9f3e72503dfa7a2f674df60b14b8b0882a24c1fef415700da00a81","label":"\u73cd\u73e0\u6751","level_id":607516458,"netversion_u16":[55327,49307,23536,0,0,25536],"placement_sha256":"6ca0aa7597f2d18684451b99faa1f59764177c2162c3631aeff1a99580c18087","resource_revision":"apk-v76318-421629","scene":"Dusk_PearlIsland","scene_provenance_sha256":"9cd32a3ddb7008b0a7ed538d3c0a2e6de0352dfa8d0b2e8c55bd0ca410bafbff","scene_sha256":"b91732fde2d4faf2ce214c379ee315fd136ac1d1e90c1d0a9c78b48060a1ed54"},"649101397":{"actor_sha256":"64c8d3ccc6c18b4c4c98f9d7e5655b1d6b16b5335f0212ddd16802e21b8fb483","cache_sha256":"7a0f6a672a9cad8730cbf05de832e12e131e0704ea81010639a066c6479515b3","label":"\u96ea\u9690\u5cf0","level_id":649101397,"netversion_u16":[2078,49307,23536,0,0,25536],"placement_sha256":"2a3870b60fe87aef6050891e82c73d242d396e599b53cb0f1da0ce8abab4ef00","resource_revision":"apk-v76318-421629","scene":"Sunset_YetiPark","scene_provenance_sha256":"bb064dddd2a5760a524aeb5eae7eb77055ed71e1f1356c5e487cea504b62f904","scene_sha256":"b70312a91a8820ddd397dc46e2ae364b792d4d0fcdffdb00660a2090708fdad7"},"748712866":{"actor_sha256":"4d3870e5f8757cd39270169477510a79b2b27fd8628dcca2019f02af9d790e0f","cache_sha256":"003f3a26cf407bc726bd9aeceb5fcbb34e12c836848856770732b406a06bb432","label":"\u9884\u8a00\u5c71\u8c37","level_id":748712866,"netversion_u16":[38306,49307,23536,0,0,25536],"placement_sha256":"9fa659894c11e0a59972693b660edea5a41d9e1796ca199f45f886a8d536cfa0","resource_revision":"apk-v76318-421629","scene":"DawnCave","scene_provenance_sha256":"cc259a3a5bfee49307ed87d09d5b902433f0d89a95b094d6dbdc3fcc4aa22eaf","scene_sha256":"ae17f7f1317e6d53174c345f0ce5a533ad4bfc6aaf3e633591126ebe36abd387"},"817373972":{"actor_sha256":"e644c9edce17de98d72ebb1b5516608dff8463fddb1723bad1c3851009b8d445","cache_sha256":"fdb50bfc1e8bc6a6596ebc8e8b4142b2c9136624ba9aa44cecddeab792d16681","label":"\u66ae\u571f\u5927\u5385","level_id":817373972,"netversion_u16":[64828,49307,23536,0,0,25536],"placement_sha256":"fced60c0b003e9e45b47717a013a1721265b21fd40312110adc7dee1ae50ab7d","resource_revision":"apk-v76318-421629","scene":"DuskStart","scene_provenance_sha256":"8f7db5b952fb06dab17cefe56bda326b8b80e4cac3549767bc81fc8d33d6dc25","scene_sha256":"171e2f69aaa6be590e1ad166281f25b77dba20a1d14d25d087cb25950c8129e8"},"864432821":{"actor_sha256":"d49dee68d0d8e7d19a1da242649fdc67d48dbec44fe0eaa0f47b0a87711aad0e","cache_sha256":"c1920bef28bca9b2ff5485518b4d5d21821b9819f039ca97893be28fc3b6b0cf","label":"\u5de8\u517d\u8352\u539f","level_id":864432821,"netversion_u16":[19939,49307,23536,0,0,25536],"placement_sha256":"e80f4457a97e340ae8eb4341cf750e634e9f1876822770d511accdb58358c474","resource_revision":"apk-v76318-421629","scene":"DuskGraveyard","scene_provenance_sha256":"de74c6dc6f375d2240db10e1bede7a3515b8e4c6e5a56f50e7f1b078eb2ebce6","scene_sha256":"54e0399e364045bd392eb243ff07e2ae5d7b61e339574027b3e2566cce4352d4"},"961583455":{"actor_sha256":"7d85220317f7bacc5237e0815b257b16d265208bf67eb271b97ee4078f7b4c58","cache_sha256":"6dbfc303533525507a72aa159d20c18dc9eca037fb828a3aeb68597dc9ac5104","label":"\u5e02\u96c6\uff08\u72ec\u7acb\u573a\u666f\uff09","level_id":961583455,"netversion_u16":[49107,49307,23536,0,0,25536],"placement_sha256":"d9f096510379c8fe3c8825f4c78c5b5860c3196bf1d081c2fe26236afbfe164d","resource_revision":"apk-v76318-421629","scene":"DuskMid_PastMarket","scene_provenance_sha256":"4589fd0d261ba529d30bcdb68daa3a6676a8128b5473931fad4e1edef0bd52e5","scene_sha256":"81e9198f8e6dcd59051da03c440d6fd10bd90583e86854ff04549a4cd08a1f38"},"98108829":{"actor_sha256":"86073f8ae030b2bf4522a92fef6ecf1743d87f566e63b483662b6a96c098f73d","cache_sha256":"4eaaadfba34b3d970ec6a8b3b0d3bb36c5e1a884fe52603504c5e6ba2c64b171","label":"\u661f\u591c\u753b\u5eca","level_id":98108829,"netversion_u16":[46138,49307,23536,0,0,25536],"placement_sha256":"625d074dff0bee0e3c33ea07a4680d82292be3d8915a774f2d9ca0cae2a012e9","resource_revision":"apk-v76318-421629","scene":"Night_GO_Gallery","scene_provenance_sha256":"eb373f8464f143d0b50074b58d31694fb0ece18fd0210cb4584bb4445fbbcadd","scene_sha256":"abc4fa78759aa1ff31769b8f9e31df00e8c6accfbfc63cf89c7dc4ce0be422af"},"996052172":{"actor_sha256":"9de045e022e8012fe5d0ce4d5762cc34db3587b8a5de93e8282d714502ff5d95","cache_sha256":"a7dad6b027c69e4cccd4bdf49e1565ee600b69a09ae2c39a951130a2241fee6b","label":"\u81f4\u68b5\u9ad8\u5b63\uff08\u5c9b\u5c7f\u573a\u666f\uff09","level_id":996052172,"netversion_u16":[23855,49307,23536,0,0,25536],"placement_sha256":"2bf49e2bba441c1415021e3b6d2845e94375c69254fbcac926abe203f7911096","resource_revision":"apk-v76318-421629","scene":"Night_GO_Island","scene_provenance_sha256":"db629724e204d5681cbaa62db80a36c55672bd9a00e37667a33e368bae6e48e0","scene_sha256":"db633890383dca7cbbb20faba46b4cc8d22c70744f4b76229ddf2da9a8ad473d"}}'
_EXPECTED_ENTRIES_SHA = '3809d063a01dcc86a83e0f52090c27520e30e28b883f98f2698f7ce3695bf278'
_EXPECTED_VARIANTS_JSON = '{"507487826":{"qianniaocheng":{"actor_sha256":"1b3539cf9d032e493023c2c6b8d30d3345c856688ea1230e567fc4d2593b856a","annotation_sha256":"db3539593747d63bac3aba5199f9092c549791aad51a55412ebeddf5bc018d73","label":"千鸟城","placement_sha256":"1cb24031f4d5132f5fe29913feadafda743527de5e628e13c76621705a6e1473","scene":"SunsetEnd2"}}}'
_EXPECTED_VARIANTS_SHA = 'c59b5cf37f443e36cf1970a85634106ce884e9f3027b917b990b97e8a7546ea7'
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
    if type(m.get('apk_build'))is not int or m['apk_build']!=421629 or m.get('native_sha256')!=NATIVE_SHA256:_fail('manifest_native_build')
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
    main_map = at('0x21dd0e8', 'raw', 4)
    origin = at('0x21dd054', 'raw', 4)
    history = at('0x21defc0', 'raw', 1)
    velocity = at('0x21dd298', 'bits', 8)
    changes = {main_map: struct.pack('<I', map_id), history+1: struct.pack('<I', map_id)}
    for i, value in enumerate(position):
        changes[origin+i] = struct.pack('<f', value)
        changes[history+2+i] = struct.pack('<f', value)
    for i in range(3):
        changes[velocity+i] = 127
    mask = bytearray(len(source))
    # 2026-11-06 好友传送：0x21df648 关卡状态头（raw2, level, resource tag, ?, bits9）可改成本图值，或保持源值。
    level_state = at('0x21df648', 'raw', 2)
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
    if (prov.get('native_sha256')!=NATIVE_SHA256 or prov.get('apk_build')!=421629 or
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
    apk_build:int=421629
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
        c.scene!=scene or c.resource_revision!=resource_revision or c.level_id!=level_id or c.native_sha256!=NATIVE_SHA256 or c.apk_build!=421629 or
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
    return {'schema':1,'kind':KIND,'classification':KIND,'apk_build':421629,'native_sha256':NATIVE_SHA256,
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
