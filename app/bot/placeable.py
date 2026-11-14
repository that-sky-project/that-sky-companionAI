"""0.16.8 道具 RPC 的有界纯解析及房间内存表；不访问网络、账号或文件。

字段名称只承诺字节布局。resource_id 是 64 个 ASCII 十六进制字符，
样本不能证明它是实例 ID；RAM key 为 owner/resource，允许同一件的更新，
同人同资源多件目前会覆盖。Destroy 的 tag 与 Placeable 相同，仅作为收窄条件。
"""
import copy
import json
import math
import re
import struct
import time
import uuid
from collections.abc import Mapping

PLACEABLE_RPC = 0xDADABFCF
DESTROY_PLACEABLE_RPC = 0xF1223F2F
MAX_GAME_BYTES = 20000
MAX_RPC_RECORDS = 512
# 房间元数据与地形 active 集分离；这里只保存有界RAM记录，不扩碰撞预算。
MAX_PROPS = 256
MAX_UNMODELED_PROPS = 256
UNMODELED_TTL_SECONDS = 600.0
MAX_KNOWN_NAMES = 2048
MAX_COORD = 10000.0
KNOWN_PROP_STATUS = {'AP04Bonfire': 'ineligible',
 'AP05Brazier_01': 'ready',
 'AP08Brazier_01': 'ready',
 'AP10Bookshelf_01': 'ready',
 'AP10Brazier_01': 'ready',
 'AP10Hammock_01': 'ineligible',
 'AP10Hoop_01': 'ineligible',
 'AP10Jar_01': 'ready',
 'AP10Pillow_01': 'ready',
 'AP10Spotlight_01': 'ambiguous',
 'AP10Tent_01': 'ineligible',
 'AP10Torch_01': 'ready',
 'AP11Fox_01': 'ineligible',
 'AP11Fox_02': 'ineligible',
 'AP11Rose_01': 'ambiguous',
 'AP11Star_01': 'ineligible',
 'AP14VaseBouquet_01': 'ambiguous',
 'AP17Chimes': 'ineligible',
 'AP17Crab': 'ineligible',
 'AP17Crab_Blue': 'ineligible',
 'AP17Crab_Green': 'ineligible',
 'AP17Crab_Purple': 'ineligible',
 'AP17Crab_Red': 'ineligible',
 'AP17Crab_Yellow': 'ineligible',
 'AP17Kettle': 'ineligible',
 'AP17Manta': 'ineligible',
 'AP17Plants': 'ready',
 'AP17UltimateMemoryBook': 'ineligible',
 'AP18Ball': 'ineligible',
 'AP19CrystalJar': 'ready',
 'AP21Basket': 'ready',
 'AP22BathTub': 'ineligible',
 'AP22BedSingle': 'ready',
 'AP22Chair': 'ready',
 'AP22DiningTable': 'ready',
 'AP22HangingLight': 'ready',
 'AP22HangingPlanter': 'ready',
 'AP22Lamp': 'ready',
 'AP22Loveseat': 'ready',
 'AP22PendantLight': 'ready',
 'AP22StepStool': 'ready',
 'AP22WallPainting': 'ready',
 'AP22WallPaintingSet': 'ready',
 'AP22WallSpiceRack': 'ready',
 'AP23Bench': 'ready',
 'AP23ClothDivider': 'ready',
 'AP23Cup': 'ineligible',
 'AP23Curtain': 'ready',
 'AP23DecorCup': 'ineligible',
 'AP23HalfRug': 'ready',
 'AP23PlaceablePianoBasic': 'ready',
 'AP23PlaceablePianoFancy': 'ineligible',
 'AP23PlaceablePianoUpright': 'ready',
 'AP23Vanity': 'ready',
 'AP23Vase': 'ready',
 'AP23WallPosterCello': 'ready',
 'AP23WallPosterConch': 'ready',
 'AP23WallPosterDuet': 'ready',
 'AP23WallPosterPiano': 'ready',
 'AP23WallPosterPianoKeys': 'ambiguous',
 'AP24Chandelier': 'ineligible',
 'AP24ClassicPlush': 'ineligible',
 'AP24ClassicPlushAlt': 'ineligible',
 'AP24Clock': 'ineligible',
 'AP24ConsoleTable': 'ready',
 'AP24Crate': 'ready',
 'AP24Desk': 'ready',
 'AP24OilLamp': 'ineligible',
 'AP24Portrait': 'ready',
 'AP24RockingChair': 'ready',
 'AP24Tent': 'ineligible',
 'AP24WallPhotos_01': 'ready',
 'AP24WallPhotos_02': 'ready',
 'AP25ClothDivider': 'ineligible',
 'AP26TreePerch': 'ready',
 'AP27ButterflyToy': 'ineligible',
 'AP27CrabFigurine': 'ineligible',
 'AP27ManateePlush': 'ineligible',
 'AP27PlantVine': 'ineligible',
 'AP27Poster': 'ineligible',
 'AP27Projector': 'ambiguous',
 'AP27RockA': 'ineligible',
 'AP27RockB': 'ineligible',
 'AP27RockC': 'ineligible',
 'AP27RockD': 'ineligible',
 'AP27RockE': 'ineligible',
 'AP27TeaStall': 'ineligible',
 'AP28MigrationBell': 'ineligible',
 'AP30BirdPlush': 'ineligible',
 'AP30ChallengeBall': 'ineligible',
 'AP30ChallengeBallDispenser': 'ineligible',
 'AP30ChallengeBouncePad': 'ineligible',
 'AP30ChallengeBouncePad_2': 'ineligible',
 'AP30ChallengeBouncePad_3': 'ineligible',
 'AP30ChallengeLantern': 'ineligible',
 'AP30ChallengePlatform': 'ineligible',
 'AP30ChallengeTarget': 'ineligible',
 'AP30ChallengeToken': 'ineligible',
 'AP31Bed': 'ready',
 'AP31BedroomChair': 'ready',
 'AP31BedroomTallTable': 'ready',
 'AP31BlossomTea': 'ineligible',
 'AP31Easel': 'ineligible',
 'AP31IrisVase': 'ready',
 'AP31PaintingBedroom': 'ineligible',
 'AP31PaintingFarm': 'ineligible',
 'AP31PaintingField': 'ineligible',
 'AP31PaintingHouse': 'ineligible',
 'AP31PaintingPortrait': 'ineligible',
 'AP31PaintingSet': 'ready',
 'AP31PaintingStarry': 'ineligible',
 'AP31PaintingSunflower': 'ineligible',
 'AP31PaintingTree': 'ineligible',
 'AP31PaintingWindmill': 'ineligible',
 'AP31SunflowerVase': 'ready',
 'AP32MessageInABottle': 'ineligible',
 'AP32PearlBoat': 'ineligible',
 'AP32PearlCouch': 'ineligible',
 'AP32Portrait': 'ready',
 'APFlagAnim': 'ineligible',
 'AnniversaryCarpet': 'ready',
 'AnniversaryMovieSeats': 'ready',
 'AnniversaryTrophyG_00': 'ineligible',
 'AnniversaryTrophyG_01': 'ineligible',
 'AnniversaryTrophyG_02': 'ineligible',
 'AnniversaryTrophyG_03': 'ineligible',
 'AnniversaryTrophyG_04': 'ineligible',
 'AnniversaryTrophyG_05': 'ineligible',
 'AnniversaryTrophyG_06': 'ineligible',
 'AnniversaryTrophyG_07': 'ineligible',
 'AnniversaryTrophyG_08': 'ineligible',
 'AnniversaryTrophyG_09': 'ineligible',
 'AnniversaryTrophyG_Generic': 'ineligible',
 'AnniversaryTrophyS_00': 'ineligible',
 'AnniversaryTrophyS_01': 'ineligible',
 'AnniversaryTrophyS_02': 'ineligible',
 'AnniversaryTrophyS_03': 'ineligible',
 'AnniversaryTrophyS_04': 'ineligible',
 'AnniversaryTrophyS_05': 'ineligible',
 'AnniversaryTrophyS_06': 'ineligible',
 'AnniversaryTrophyS_07': 'ineligible',
 'AnniversaryTrophyS_08': 'ineligible',
 'AnniversaryTrophyS_09': 'ineligible',
 'AnniversaryTrophyS_10': 'ineligible',
 'AnniversaryTrophyS_Generic': 'ineligible',
 'BalloonArch': 'ineligible',
 'BalloonRed': 'ineligible',
 'Balloon_01': 'ineligible',
 'BeachBall': 'ineligible',
 'BeachChairCloth': 'ready',
 'BeachChairWood': 'ready',
 'BeachRecliner': 'ready',
 'Before_FruitOrangePopulate_VistaDemo1': 'ineligible',
 'BigMantaPlush': 'ineligible',
 'BirthdayCake_01': 'ineligible',
 'BirthdayCake_02': 'ineligible',
 'BirthdayCake_03': 'ineligible',
 'BirthdayCake_Giant': 'ineligible',
 'BirthdayFlags_01': 'ineligible',
 'BirthdayOreo': 'ineligible',
 'BloomSapling': 'ineligible',
 'BlueSwing': 'ineligible',
 'BoatRaceHammock': 'ineligible',
 'Brick': 'ready',
 'BubbleBeanBag': 'ready',
 'Bucket': 'ineligible',
 'ButterflyFountain': 'ineligible',
 'CandleFruit': 'ineligible',
 'Cannon_01': 'ineligible',
 'Cannon_Pin': 'ineligible',
 'CeilingYellow': 'ineligible',
 'ChallengeTreasure': 'ineligible',
 'ClosedAnnivLaptop': 'ineligible',
 'CloudMusicHomeListening': 'ineligible',
 'CloudMusicTable': 'ineligible',
 'CocoaCup': 'ineligible',
 'ColorKiteBird': 'ready',
 'ColorKiteButterfly': 'ready',
 'ColorKiteCrab': 'ineligible',
 'ColorKiteJelly': 'ready',
 'ColorKiteLighthorn': 'ready',
 'ColorKiteManatee': 'ready',
 'ColorKiteManta': 'ready',
 'ColorKiteTunaKing': 'ineligible',
 'ColorKiteTurtle': 'ready',
 'ColorfulSilkBall': 'ineligible',
 'ColorfulSilkBallDispenser': 'ineligible',
 'CompanionCube': 'ineligible',
 'CrabFloat': 'ineligible',
 'CrabOfCompetition_Gold': 'ineligible',
 'CrabOfCompetition_Stone': 'ineligible',
 'CrabbitPortal': 'ambiguous',
 'CurtainDoorYellow': 'ineligible',
 'DecorFoldedCloth': 'ready',
 'DecorPillowA': 'ready',
 'DecorPillowB': 'ready',
 'DiscoLight': 'ineligible',
 'DragonBoat': 'ineligible',
 'FetchToy': 'ineligible',
 'FishE': 'ineligible',
 'FlowerArch': 'ineligible',
 'FortuneCandleFlags': 'ineligible',
 'FortuneCarousel': 'ineligible',
 'FortuneCoupletPoster': 'ineligible',
 'FortunePlant': 'ineligible',
 'FriendshipDuckSofa': 'ineligible',
 'HangingLights': 'ineligible',
 'HangingMask': 'ineligible',
 'HeartPlush': 'ineligible',
 'HidenSeekBackpack': 'ineligible',
 'HidenSeekBackpackPile': 'ineligible',
 'HidenSeekBirthdayOreo': 'ineligible',
 'HidenSeekBonfire': 'ineligible',
 'HidenSeekClayJar': 'ineligible',
 'HidenSeekCocoaStand': 'ineligible',
 'HidenSeekCrate': 'ineligible',
 'HidenSeekFabricRoll': 'ineligible',
 'HidenSeekFence': 'ineligible',
 'HidenSeekFirewood': 'ineligible',
 'HidenSeekFirework': 'ineligible',
 'HidenSeekFireworkCrate': 'ineligible',
 'HidenSeekFireworkHuge': 'ineligible',
 'HidenSeekGiftCrate': 'ineligible',
 'HidenSeekJarCarpet': 'ineligible',
 'HidenSeekNailCrate': 'ineligible',
 'HidenSeekPillowXmas': 'ready',
 'HidenSeekSnowBallHuge': 'ineligible',
 'HidenSeekSnowman': 'ineligible',
 'HidenSeekSnowmanHuge': 'ineligible',
 'HidenSeekStoneBench': 'ineligible',
 'HidenSeekStoneCafeTable': 'ineligible',
 'HidenSeekStoneJar': 'ineligible',
 'HidenSeekStreetLamp': 'ineligible',
 'HidenSeekTeaStall': 'ineligible',
 'HidenSeekWingBuffChild': 'ineligible',
 'HidenSeekWoodPile': 'ineligible',
 'HidenSeekWoodenBarrel': 'ineligible',
 'HidenSeekWoodenCrate': 'ineligible',
 'IceBathtubLarge': 'ineligible',
 'IceBedSingle': 'ready',
 'IceBlockHorizontal': 'ready',
 'IceBlockVertical': 'ready',
 'IceConsoleTable': 'ready',
 'IceDresserTall': 'ready',
 'IceKitchenDrawers': 'ready',
 'IceKitchenOven': 'ready',
 'IceKitchenStove': 'ready',
 'IceSlideConcave': 'ready',
 'IceSlideConvex': 'ready',
 'IceSofaSide': 'ready',
 'IceStool': 'ready',
 'IceTableSmall': 'ready',
 'InstrumentRack': 'ineligible',
 'IslandUmbrella_01': 'ineligible',
 'IvorySwing': 'ineligible',
 'JarBubbleMachine': 'ineligible',
 'JarRound': 'ready',
 'JarTall': 'ready',
 'JuiceCup': 'ineligible',
 'KizunaAiTVPillow': 'ambiguous',
 'KrillCarpet': 'ineligible',
 'LanternPost': 'ready',
 'LargeBonfire': 'ineligible',
 'LemonadeStand': 'ineligible',
 'LevelPizzahut': 'ineligible',
 'LightFence_01': 'ineligible',
 'LotusBathtub': 'ineligible',
 'LoveBoat': 'ineligible',
 'MCAxolotl': 'ineligible',
 'MannequinFigurine': 'ambiguous',
 'MantaFloat': 'ineligible',
 'MarshmallowSet': 'ineligible',
 'MeteorHeart': 'ineligible',
 'MicStandBronze': 'ineligible',
 'MicStandGold': 'ineligible',
 'MicStandSilver': 'ineligible',
 'MicStandWhite': 'ineligible',
 'MischiefBoboCandle': 'ready',
 'MischiefCat': 'ineligible',
 'MischiefCat_02': 'ineligible',
 'MischiefCauldron': 'ineligible',
 'MischiefCoatRack': 'ready',
 'MischiefCrabSeat': 'no_geometry',
 'MischiefPumpkin': 'ineligible',
 'MischiefPumpkinLamp': 'ready',
 'MischiefPumpkinSofa': 'ready',
 'MischiefTable': 'ready',
 'MischiefTree': 'ready',
 'MischiefWeb': 'ready',
 'MooncakePlate': 'ineligible',
 'MoonlightLantern': 'ineligible',
 'MoonlightLotusCushion': 'ineligible',
 'MoonlightMural_Butterfly01': 'ineligible',
 'MoonlightMural_Cloud01': 'ineligible',
 'MoonlightMural_Cloud02': 'ineligible',
 'MoonlightMural_Cloud03': 'ineligible',
 'MoonlightMural_Cloud04': 'ineligible',
 'MoonlightMural_Deer01': 'ineligible',
 'MoonlightMural_Deer02': 'ineligible',
 'MoonlightMural_Deer03': 'ineligible',
 'MoonlightMural_Flower01': 'ineligible',
 'MoonlightMural_Flower02': 'ineligible',
 'MoonlightMural_Flower03': 'ineligible',
 'MoonlightMural_Flower04': 'ineligible',
 'MoonlightMural_Fly01': 'ineligible',
 'MoonlightMural_Fly02': 'ineligible',
 'MoonlightMural_Halo': 'ineligible',
 'MoonlightMural_Jellyfish01': 'ineligible',
 'MoonlightMural_Jellyfish02': 'ineligible',
 'MoonlightMural_Jellyfish03': 'ineligible',
 'MoonlightMural_Line01': 'ineligible',
 'MoonlightMural_Line02': 'ineligible',
 'MoonlightMural_Line03': 'ineligible',
 'MoonlightMural_Manta01': 'ineligible',
 'MoonlightMural_Manta02': 'ineligible',
 'MoonlightMural_Manta03': 'ineligible',
 'MoonlightMural_Manta04': 'ineligible',
 'MoonlightMural_Manta05': 'ineligible',
 'MoonlightMural_Moon': 'ambiguous',
 'MoonlightMural_Mountain01': 'ineligible',
 'MoonlightMural_Mountain02': 'ineligible',
 'MoonlightMural_Mountain03': 'ineligible',
 'MoonlightMural_People01': 'ineligible',
 'MoonlightMural_People02': 'ineligible',
 'MoonlightMural_Star01': 'ineligible',
 'MoonlightMural_Star02': 'ineligible',
 'MoonlightMural_Star03': 'ineligible',
 'MoonlightMural_Star04': 'ineligible',
 'MoonlightPoster': 'ready',
 'MuralAir': 'ambiguous',
 'MuralDark': 'ambiguous',
 'MuralEarth': 'ambiguous',
 'MuralFire': 'ambiguous',
 'MuralLight': 'ambiguous',
 'MuralMind': 'ambiguous',
 'MuralVoid': 'ambiguous',
 'MuralWater': 'ambiguous',
 'MusicShell': 'ineligible',
 'MusicShellOffice': 'ineligible',
 'NatureClamShell': 'ready',
 'NatureFlag': 'ready',
 'NatureSculptureA': 'ready',
 'NatureSculptureB': 'ready',
 'NatureSculptureC': 'ready',
 'NightBook': 'ready',
 'OreoPlush': 'ineligible',
 'PastryPlush': 'ineligible',
 'PastryTeaTable': 'ambiguous',
 'PianoUpright': 'ready',
 'PicnicBlanket': 'ineligible',
 'PillowXmas': 'ready',
 'Pinwheel_01': 'ineligible',
 'Pinwheel_Red': 'ineligible',
 'PipeBroken_01': 'ineligible',
 'Pizzahut': 'ineligible',
 'PlaceableMessage': 'ineligible',
 'PlaceableRace': 'ineligible',
 'PlaceableTokenPast': 'ineligible',
 'PropHammer_01': 'ineligible',
 'PuzzleBox': 'ineligible',
 'PuzzleCage': 'ineligible',
 'PuzzleChest': 'ineligible',
 'PuzzleKey': 'ineligible',
 'PuzzlePressurePlate': 'ineligible',
 'QixiBird': 'ineligible',
 'Rainbow': 'ready',
 'RibbonHourglass': 'ineligible',
 'RoseJar': 'ineligible',
 'RugLargeCircle': 'ready',
 'RugLargeSolid': 'ready',
 'RugMediumArgyle': 'ready',
 'RugMediumCircle': 'ready',
 'RugMediumDiamonds': 'ready',
 'RugMediumSolid': 'ready',
 'RugMediumStripes': 'ready',
 'RugSmallClassic': 'ready',
 'RugSmallHalfCircle': 'ready',
 'RugSmallSolid': 'ready',
 'RugSmallSolid_AP23Cellist': 'ineligible',
 'RugSmallSolid_AP23Pianist': 'ineligible',
 'RugSmallStripes': 'ready',
 'ScreenBlue': 'ready',
 'ScreenGreen': 'ready',
 'ScreenRed': 'ready',
 'ScreenYellow': 'ready',
 'Seesaw': 'ineligible',
 'SequencerAnni': 'ineligible',
 'SequencerStation': 'ineligible',
 'ShellMusicBox': 'ineligible',
 'SkyArtBook01_Closed': 'ineligible',
 'SkyArtBook02_Closed': 'ineligible',
 'SkyArtBookBlueNE_Closed': 'ineligible',
 'SkyBallGoals': 'ineligible',
 'SkyBallGoalsLaundry': 'ineligible',
 'SkyBallGoalsWood': 'ineligible',
 'SkyBalloon': 'ineligible',
 'SkyStoryBook01_Closed': 'ineligible',
 'SkykidPlushie': 'ineligible',
 'SnowGlobe': 'ineligible',
 'Snowman': 'ineligible',
 'SnowmanArm_01': 'ready',
 'SnowmanArm_02': 'ready',
 'SnowmanBeard': 'ineligible',
 'SnowmanBodyLarge': 'ready',
 'SnowmanBodyMedium': 'ready',
 'SnowmanBodySmall': 'ready',
 'SnowmanButton': 'ineligible',
 'SnowmanElk': 'ineligible',
 'SnowmanEye': 'ineligible',
 'SnowmanHead': 'ready',
 'SnowmanHolly': 'ineligible',
 'SnowmanMouth': 'ineligible',
 'SnowmanNose': 'ineligible',
 'SnowmanSantaHat': 'ineligible',
 'SnowmanScarf': 'ineligible',
 'SnowmanYeti': 'ineligible',
 'Sparkler': 'ineligible',
 'SpellFire': 'ineligible',
 'StarJar': 'ineligible',
 'Steamer': 'ineligible',
 'StoneArmchair': 'ready',
 'StoneBathtubLarge': 'ineligible',
 'StoneBathtubLargeIceNe': 'ineligible',
 'StoneBathtubSmall': 'ineligible',
 'StoneBedSingle': 'ready',
 'StoneBedSingleIceNe': 'ready',
 'StoneBench': 'ready',
 'StoneBenchSingle': 'ready',
 'StoneBoxClosed': 'ineligible',
 'StoneBoxEmpty': 'ready',
 'StoneCandleHolder': 'ready',
 'StoneCandleLight': 'ready',
 'StoneChair': 'ready',
 'StoneCoffeeTable': 'ready',
 'StoneConsoleTable': 'ready',
 'StoneConsoleTableIceNe': 'ready',
 'StoneCubeSmall': 'ready',
 'StoneCubeTall': 'ready',
 'StoneCubeWide': 'ready',
 'StoneDesk': 'ready',
 'StoneDiningTableLong': 'ready',
 'StoneDiningTableRound': 'ready',
 'StoneDiningTableSquare': 'ambiguous',
 'StoneDresserLong': 'ineligible',
 'StoneDresserTall': 'ready',
 'StoneDresserTallIceNe': 'ready',
 'StoneKitchenCabinet': 'ready',
 'StoneKitchenDrawers': 'ready',
 'StoneKitchenDrawersIceNe': 'ready',
 'StoneKitchenOven': 'ambiguous',
 'StoneKitchenOvenIceNe': 'ready',
 'StoneKitchenSink': 'ineligible',
 'StoneKitchenStove': 'ambiguous',
 'StoneKitchenStoveIceNe': 'ready',
 'StoneLoveseat': 'ready',
 'StoneMannequin': 'ineligible',
 'StonePlantStand': 'ready',
 'StoneSink': 'ineligible',
 'StoneSofaCorner': 'ready',
 'StoneSofaSide': 'ready',
 'StoneSofaSideIceNe': 'ready',
 'StoneStool': 'ready',
 'StoneStoolIceNe': 'ready',
 'StoneTableSmall': 'ready',
 'StoneTableSmallIceNe': 'ready',
 'StoneWallMirror': 'ready',
 'StoneWallMugRack': 'ready',
 'StoneWallPotRack': 'ready',
 'StoneWallSconce': 'ready',
 'StoneWallShelf': 'ready',
 'StoneWallTowelRack': 'ready',
 'Stool': 'ready',
 'StupaBell': 'ineligible',
 'Sunflower': 'ineligible',
 'SunflowerBarTable': 'ready',
 'SunflowerBed': 'ready',
 'SunflowerLadder': 'ineligible',
 'SunflowerLamp': 'ineligible',
 'SunflowerPillow': 'ready',
 'SunflowerRugHalfCircle': 'ready',
 'SunflowerScreen': 'ineligible',
 'SunflowerWallShelf': 'ready',
 'SunflowerWreath': 'ready',
 'SunlightProjector': 'ineligible',
 'SunlightRamp': 'ineligible',
 'SunlightSandcastleA': 'ineligible',
 'SunlightSandcastleB': 'ineligible',
 'SunlightSandcastleC': 'ineligible',
 'SunlightSandcastleD': 'ineligible',
 'SunlightSandcastleE': 'ineligible',
 'SunlightSandcastleF': 'ineligible',
 'SunlightSandcastleG': 'ineligible',
 'SunlightSurfboardSporty': 'ambiguous',
 'Surfboard': 'ambiguous',
 'Swing': 'ineligible',
 'Table': 'ready',
 'TableBronze': 'ineligible',
 'TableGold': 'ineligible',
 'TableSilver': 'ineligible',
 'TableXmas': 'ready',
 'TeaBath': 'ineligible',
 'TeaTable': 'ready',
 'Tent_01': 'ineligible',
 'TreasurePirateBed': 'ready',
 'TreasureSeekersBanner': 'ready',
 'TriumphFlagDusk': 'ineligible',
 'TriumphFlagPrairie': 'ineligible',
 'TriumphFlagRain': 'ineligible',
 'TriumphFlagSunset': 'ineligible',
 'TriumphPodium': 'ineligible',
 'TwinRibbonOrnament': 'ineligible',
 'TwinRibbonPoster': 'ineligible',
 'UmbrellaCup': 'ineligible',
 'UnitCube': 'ready',
 'UnitCylinder': 'ineligible',
 'UnitSphere': 'ineligible',
 'WerewolveTable': 'ineligible',
 'WindowYellow': 'ineligible',
 'WisteriaTeaTable': 'ambiguous',
 'Wooden_Cabinet': 'no_geometry',
 'Wooden_Diningtable': 'no_geometry',
 'Wooden_Shelf': 'no_geometry'}
KNOWN_NAMES = frozenset(KNOWN_PROP_STATUS)
KNOWN_GEOMETRY_NAMES = frozenset(name for name, status in KNOWN_PROP_STATUS.items() if status == "ready")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,95}\Z")
_RESOURCE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _string(body, offset, limit):
    if offset + 4 > len(body):
        raise ValueError("prop_truncated_string_length")
    size = struct.unpack_from("<I", body, offset)[0]
    start, end = offset + 4, offset + 4 + size
    if not 0 < size <= limit or end > len(body):
        raise ValueError("prop_string_bounds")
    try:
        return body[start:end].decode("utf-8"), end
    except UnicodeDecodeError:
        raise ValueError("prop_string_encoding") from None


def parse_placeable_body(body):
    if type(body) is not bytes or not 85 <= len(body) <= 1200:
        raise ValueError("prop_body_length")
    text, offset = _string(body, 0, 1024)
    try:
        metadata = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("prop_metadata_json") from None
    if (type(metadata) is not dict or type(metadata.get("name")) is not str
            or not _NAME.fullmatch(metadata["name"])):
        raise ValueError("prop_name_invalid")
    resource_id, offset = _string(body, offset, 64)
    if not _RESOURCE.fullmatch(resource_id) or len(body) - offset != 77:
        raise ValueError("prop_profile_mismatch")
    owner_slot = body[offset + 9]
    owner_uuid = str(uuid.UUID(bytes=body[offset + 10:offset + 26]))
    position = struct.unpack_from("<3f", body, offset + 26)
    rotation = struct.unpack_from("<4f", body, offset + 38)
    scale = struct.unpack_from("<f", body, offset + 57)[0]
    if not owner_slot or owner_uuid == str(uuid.UUID(int=0)):
        raise ValueError("prop_owner_invalid")
    if (not all(math.isfinite(x) and abs(x) <= MAX_COORD for x in position)
            or not all(math.isfinite(x) for x in rotation)
            or not 0.5 <= sum(x * x for x in rotation) <= 1.5
            or not math.isfinite(scale) or not 0.05 <= scale <= 10.0):
        raise ValueError("prop_transform_invalid")
    tag = body[offset + 61:offset + 64].hex()
    return {
        "kind": "place", "id": owner_uuid + "/" + resource_id.lower(),
        "name": metadata["name"], "metadata": metadata,
        "resource_id": resource_id.lower(), "owner_slot": owner_slot,
        "owner_uuid": owner_uuid, "position": list(position),
        "rotation": list(rotation), "scale": scale,
        "state": body[offset + 56], "tag": tag,
        "timestamp_u32": struct.unpack_from("<I", body, offset + 64)[0],
        "raw_unknown": {
            "prefix9": body[offset:offset + 9].hex(),
            "rotation_suffix2": body[offset + 54:offset + 56].hex(),
            "suffix9": body[offset + 68:offset + 77].hex(),
        },
    }


def parse_destroy_body(body):
    if type(body) is not bytes or len(body) != 24:
        raise ValueError("prop_destroy_profile_mismatch")
    owner_slot, owner_uuid = body[0], str(uuid.UUID(bytes=body[1:17]))
    if not owner_slot or owner_uuid == str(uuid.UUID(int=0)):
        raise ValueError("prop_owner_invalid")
    return {"kind": "destroy", "owner_slot": owner_slot, "owner_uuid": owner_uuid,
            "tag": body[21:24].hex(), "raw_unknown": {"owner_suffix4": body[17:21].hex()}}


def parse_game(body):
    """先验证完整 NetRpc 链，再解码目标记录；无签名字节搜索和部分链发布。

    返回 (事件列表, 被忽略的目标记录诊断)。无关 RPC 按其长度跳过。
    容器损坏 raise ValueError，单个道具字段损坏跳过该条并返回诊断。
    """
    if type(body) is not bytes or not 4 <= len(body) <= MAX_GAME_BYTES or body[0] != 2:
        raise ValueError("prop_game_header_invalid")
    records, offset = [], 4
    while offset < len(body):
        if offset + 7 > len(body) or len(records) >= MAX_RPC_RECORDS:
            raise ValueError("prop_rpc_chain_invalid")
        key, flags, size = struct.unpack_from("<IBH", body, offset)
        end = offset + 7 + size
        if end > len(body):
            raise ValueError("prop_rpc_chain_truncated")
        records.append((offset, key, flags, body[offset + 7:end]))
        offset = end
    events, ignored = [], []
    for offset, key, flags, data in records:
        if key not in (PLACEABLE_RPC, DESTROY_PLACEABLE_RPC):
            continue
        try:
            if flags != (1 if key == PLACEABLE_RPC else 0):
                raise ValueError("prop_rpc_flags_unknown")
            event = parse_placeable_body(data) if key == PLACEABLE_RPC else parse_destroy_body(data)
        except (ValueError, struct.error) as exc:
            ignored.append({"offset": offset, "hash": key, "reason": str(exc)})
            continue
        event.update(rpc_offset=offset, rpc_flags=flags, epoch=body[1], source_slot=body[2])
        events.append(event)
    return events, ignored


class PlaceableRegistry:
    """由 RoomTracker.lock 保护；单独使用时调用方负责串行化。"""
    def __init__(self, known_names=KNOWN_NAMES, *, clock=None):
        self.known_names = frozenset(known_names)
        defaults = globals().get("KNOWN_PROP_STATUS", {}) if self.known_names == KNOWN_NAMES else {}
        self.geometry_names = frozenset(name for name in self.known_names if defaults.get(name, "ready") == "ready")
        self.geometry_status = {name: defaults.get(name, "ready") for name in self.known_names}
        self.props = {}
        self.revision = 0
        # 仅保留有界放置元数据用于交互/疑似支撑；永不交给碰撞builder。
        self.unmodeled_props = {}
        self.unmodeled_revision = 0
        self._clock = clock or time.monotonic

    def clear(self):
        self.props.clear()
        self.unmodeled_props.clear()
        self.unmodeled_revision += 1
        self.revision += 1  # 空表复位也失效正在进行的地形同步。

    def retain_owners(self, owners):
        owners = set(owners)
        removed = [key for key, prop in self.props.items() if prop["owner_uuid"] not in owners]
        for key in removed:
            del self.props[key]
        if removed:
            self.revision += 1
        unmodeled = [key for key, prop in self.unmodeled_props.items() if prop["owner_uuid"] not in owners]
        for key in unmodeled:del self.unmodeled_props[key]
        if unmodeled:self.unmodeled_revision += 1
        return len(removed)+len(unmodeled)

    def _expire_unmodeled(self):
        now=float(self._clock())
        expired=[key for key,value in self.unmodeled_props.items()
                 if not 0<=now-value['observed_mono']<=UNMODELED_TTL_SECONDS]
        for key in expired:del self.unmodeled_props[key]
        if expired:self.unmodeled_revision += 1
        return now

    def _remember_unmodeled(self, event, reason):
        now=self._expire_unmodeled()
        try:
            position,rotation=event['position'],event['rotation']
            if (type(event['name']) is not str or not _NAME.fullmatch(event['name'])
                    or len(position)!=3 or len(rotation)!=4
                    or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>MAX_COORD for v in position)
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in rotation)
                    or not .5<=sum(v*v for v in rotation)<=1.5
                    or type(event['scale']) not in (int,float) or not .05<=event['scale']<=10.):
                return False
            key=event['id']
            if key not in self.unmodeled_props and len(self.unmodeled_props)>=MAX_UNMODELED_PROPS:
                return False
            value={k:copy.deepcopy(v) for k,v in event.items()
                   if k not in ('kind','rpc_offset','rpc_flags','epoch','source_slot')}
            value.update(observation_only=True,geometry_status=reason,observed_mono=now,
                         expires_mono=now+UNMODELED_TTL_SECONDS)
            self.unmodeled_props[key]=value
            self.unmodeled_revision += 1
            return True
        except (KeyError,TypeError,ValueError,OverflowError):
            return False

    def set_known_names(self, names):
        supplied = names
        names = frozenset(names)
        if len(names) > MAX_KNOWN_NAMES or any(type(n) is not str or not _NAME.fullmatch(n) for n in names):
            raise ValueError("prop_allowed_names_invalid")
        if isinstance(supplied, Mapping):
            statuses = dict(supplied)
            if any(value not in ("ready", "no_geometry", "ineligible", "ambiguous") for value in statuses.values()):
                raise ValueError("prop_geometry_status_invalid")
        else:
            statuses = {name: "ready" for name in names}
        # 明确目录子集只改变几何可用性，不能把已确认 APK 名称伪报为未知名称。
        known = names | KNOWN_NAMES
        if len(known) > MAX_KNOWN_NAMES:
            raise ValueError("prop_allowed_names_invalid")
        self.known_names = known
        self.geometry_names = frozenset(name for name, status in statuses.items() if status == "ready")
        self.geometry_status = {name: statuses.get(name, "no_geometry") for name in known}
        removed = [key for key, prop in self.props.items() if prop["name"] not in self.geometry_names]
        for key in removed:
            self._remember_unmodeled(self.props[key],self.geometry_status.get(self.props[key]['name'],'no_geometry'))
            del self.props[key]
        if removed:
            self.revision += 1

    def apply(self, events, slots):
        """只接受当前 roster 的 slot/UUID 绑定；返回有界诊断，不写日志文件。"""
        ignored = []
        self._expire_unmodeled()
        for event in events:
            if slots.get(event["owner_slot"]) != event["owner_uuid"]:
                ignored.append({"reason": "prop_owner_not_in_roster"})
                continue
            if event["kind"] == "destroy":
                removed = [key for key, prop in self.props.items()
                           if prop["owner_uuid"] == event["owner_uuid"] and prop["tag"] == event["tag"]]
                for key in removed:
                    del self.props[key]
                if removed:
                    self.revision += 1
                unmodeled=[key for key,prop in self.unmodeled_props.items()
                           if prop['owner_uuid']==event['owner_uuid'] and prop['tag']==event['tag']]
                for key in unmodeled:del self.unmodeled_props[key]
                if unmodeled:self.unmodeled_revision += 1
                continue
            if event["name"] not in self.known_names:
                self._remember_unmodeled(event,'unknown_name')
                ignored.append({"reason": "prop_unknown_name", "name": event["name"]})
                continue
            if event["name"] not in self.geometry_names:
                # 认得名字但没有可靠/预算内网格：不占实例槽，不推进几何版本。
                ignored.append({"reason": "prop_no_geometry", "name": event["name"],
                                "geometry_status": self.geometry_status.get(event["name"], "no_geometry")})
                self._remember_unmodeled(event,self.geometry_status.get(event['name'],'no_geometry'))
                continue
            key = event["id"]
            if key not in self.props and len(self.props) >= MAX_PROPS:
                ignored.append({"reason": "prop_limit"})
                self._remember_unmodeled(event,'room_metadata_budget')
                continue
            # RPC 容器位置、同步次数不是几何变化，不触发热地形重建。
            value = {k: copy.deepcopy(v) for k, v in event.items()
                     if k not in ("kind", "rpc_offset", "rpc_flags", "epoch", "source_slot")}
            previous = self.props.get(key)
            self.props[key] = value
            if self.unmodeled_props.pop(key,None) is not None:self.unmodeled_revision += 1
            geometry_fields = ("id", "name", "position", "rotation", "scale", "state")
            if previous is None or any(previous[field] != value[field] for field in geometry_fields):
                self.revision += 1
        return ignored

    def snapshot(self):
        return copy.deepcopy(list(self.props.values()))

    def unmodeled_snapshot(self):
        self._expire_unmodeled()
        return copy.deepcopy(list(self.unmodeled_props.values()))
