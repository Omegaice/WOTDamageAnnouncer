import BigWorld, Math
import functools
import weakref, random
from itertools import izip
from AvatarInputHandler import ShakeReason
import SoundGroups
from debug_utils import *
import constants
from constants import VEHICLE_HIT_EFFECT
import helpers
from items import vehicles
import VehicleAppearance
from gui import game_control
from gui.WindowsManager import g_windowsManager
import AreaDestructibles
import DestructiblesCache
import math
import nations
import physics_shared
import ArenaType
import BattleReplay
import TriggersManager
import json
import os
from messenger import MessengerEntry
from TriggersManager import TRIGGER_TYPE
from ModelHitTester import segmentMayHitVehicle
from gun_rotation_shared import decodeGunAngles
from constants import DESTRUCTIBLE_MATKIND, SPT_MATKIND
from material_kinds import EFFECT_MATERIAL_INDEXES_BY_IDS, EFFECT_MATERIAL_INDEXES_BY_NAMES, EFFECT_MATERIALS

class Vehicle(BigWorld.Entity):
    hornMode = property(lambda self: self.__hornMode)
    isEnteringWorld = property(lambda self: self.__isEnteringWorld)

    __damageCfg = None
    try:
        from xml.dom import minidom
        path_items = minidom.parse(os.path.join(os.getcwd(), 'paths.xml')).getElementsByTagName('Path')
        for root in path_items:
            path = os.path.join(os.getcwd(), root.childNodes[0].data)
            if os.path.isdir(path):
                conf_file = os.path.join(path, 'scripts', 'client', 'vehicle_damage.json')
                if os.path.isfile(conf_file):
                    with open(conf_file) as data_file:
                        __damageCfg = json.load(data_file)
    except Exception, err:
        LOG_NOTE("Error: ", err)

    __tankHealth = {}

    def __init__(self):
        self.proxy = weakref.proxy(self)
        self.extras = {}
        self.typeDescriptor = None
        self.appearance = None
        self.isPlayer = False
        self.isStarted = False
        self.__prereqs = None
        self.__hornSounds = (None,)
        self.__hornMode = ''
        self.__stopHornSoundCallback = None
        self.wgPhysics = None
        self.__isEnteringWorld = False
        self.__battleID = 0
        self.__hitType = 0
        self.__hasWarned = False

    def reload(self):
        wasStarted = self.isStarted
        if self.isStarted:
            self.stopVisual()
        vehicles.reload()
        self.typeDescriptor = vehicles.VehicleDescr(compactDescr=self.publicInfo.compDescr)
        if wasStarted:
            self.appearance = VehicleAppearance.VehicleAppearance()
            self.appearance.prerequisites(self)
            self.startVisual()

    def prerequisites(self):
        if self.typeDescriptor is not None:
            return ()

        prereqs = []
        descr = vehicles.VehicleDescr(compactDescr=_stripVehCompDescrIfRoaming(self.publicInfo.compDescr))
        self.typeDescriptor = descr
        prereqs += descr.prerequisites()
        for hitTester in descr.getHitTesters():
            if hitTester.bspModelName is not None and not hitTester.isBspModelLoaded():
                prereqs.append(hitTester.bspModelName)

        self.appearance = VehicleAppearance.VehicleAppearance()
        prereqs += self.appearance.prerequisites(self)
        return prereqs

    def onEnterWorld(self, prereqs):
        self.__isEnteringWorld = True
        descr = self.typeDescriptor
        descr.keepPrereqs(prereqs)
        self.__prereqs = prereqs
        self.__prevDamageStickers = frozenset()
        self.__prevPublicStateModifiers = frozenset()
        self.targetFullBounds = True
        player = BigWorld.player()
        for hitTester in descr.getHitTesters():
            hitTester.loadBspModel()
            player.hitTesters.add(hitTester)

        player.initSpace()
        player.vehicle_onEnterWorld(self)

        # Setup Tank Parameters
        for key, vehicle in player.arena.vehicles.iteritems():
            if vehicle["name"] == self.publicInfo.name:
                self.__battleID = key
                break
        if self.__battleID not in self.__tankHealth:
            self.__tankHealth[self.__battleID] = self.typeDescriptor.maxHealth

        self.__isEnteringWorld = False

    def onLeaveWorld(self):
        self.__stopExtras()
        BigWorld.player().vehicle_onLeaveWorld(self)
        assert not self.isStarted

    def showShooting(self, burstCount, isPredictedShot = False):
        if not self.isStarted:
            return
        else:
            if not isPredictedShot and self.isPlayer and not BigWorld.player().isWaitingForShot:
                if not BattleReplay.g_replayCtrl.isPlaying:
                    return
            extra = self.typeDescriptor.extrasDict['shoot']
            data = self.extras.get(extra.index)
            if data is not None:
                extra.stop(data)
            extra.startFor(self, burstCount)
            if not isPredictedShot and self.isPlayer:
                BigWorld.player().cancelWaitingForShot()

    def showDamageFromShot(self, attackerID, points, effectsIndex):
        if not self.isStarted:
            return
        else:
            self.__hitType = effectsIndex
            descr = self.typeDescriptor
            effectsDescr = vehicles.g_cache.shotEffects[effectsIndex]
            firstHitDir = None
            maxHitEffectCode = None
            hasPiercedHit = False
            for point in points:
                compName, hitEffectCode, startPoint, endPoint = _decodeSegment(descr, point)
                if startPoint == endPoint:
                    continue
                if maxHitEffectCode is None or hitEffectCode > maxHitEffectCode:
                    maxHitEffectCode = hitEffectCode
                    if not hasPiercedHit:
                        hasPiercedHit = maxHitEffectCode >= VEHICLE_HIT_EFFECT.ARMOR_PIERCED
                    stages, effects, _ = effectsDescr[self.__hitEffectCodeToEffectGroup[hitEffectCode]]
                    hitTester = getattr(descr, compName)['hitTester']
                    hitTestRes = hitTester.localHitTest(startPoint, endPoint)
                    if not hitTestRes:
                        continue
                    minDist = hitTestRes[0]
                    for hitTestRes in hitTestRes:
                        dist = hitTestRes[0]
                        if dist < minDist:
                            minDist = dist

                    dir = endPoint - startPoint
                    dir.normalise()
                    rot = Math.Matrix()
                    rot.setRotateYPR((dir.yaw, dir.pitch, 0.0))
                    mat = Math.Matrix()
                    mat.setTranslate(startPoint + dir * minDist)
                    mat.preMultiply(rot)
                    showFullscreenEffs = self.isPlayer and self.isAlive()
                    self.appearance.modelsDesc[compName]['boundEffects'].addNew(mat, effects, stages, isPlayer=self.isPlayer, showShockWave=showFullscreenEffs, showFlashBang=showFullscreenEffs)
                    if firstHitDir is None:
                        compMatrix = Math.Matrix(self.appearance.modelsDesc[compName]['model'].matrix)
                    firstHitDir = compMatrix.applyVector(dir)
                    self.appearance.receiveShotImpulse(firstHitDir, effectsDescr['targetImpulse'])
                    self.appearance.executeHitVibrations(maxHitEffectCode)
                    player = BigWorld.player()
                    player.inputHandler.onVehicleShaken(self, compMatrix.translation, firstHitDir, effectsDescr['caliber'], ShakeReason.HIT if hasPiercedHit else ShakeReason.HIT_NO_DAMAGE)

            if not hasPiercedHit:
                self.damage_announce(attackerID, 0)

            if not self.isAlive():
                return
            if attackerID == BigWorld.player().playerVehicleID:
                if maxHitEffectCode is not None and not self.isPlayer:
                    marker = getattr(self, 'marker', None)
                    manager = marker is not None and g_windowsManager.battleWindow.vMarkersManager
                    manager.updateMarkerState(marker, 'hit_pierced' if hasPiercedHit else 'hit')

    __hitEffectCodeToEffectGroup = ('armorRicochet', 'armorResisted', 'armorHit', 'armorHit', 'armorCriticalHit')

    def showDamageFromExplosion(self, attackerID, center, effectsIndex):
        if not self.isStarted:
            return
        else:
            self.__hitType = effectsIndex
            impulse = vehicles.g_cache.shotEffects[effectsIndex]['targetImpulse']
            dir = self.position - center
            dir.normalise()
            self.appearance.receiveShotImpulse(dir, impulse / 4.0)
            self.appearance.executeHitVibrations(VEHICLE_HIT_EFFECT.MAX_CODE + 1)
            if not self.isAlive():
                return
            if self.id == attackerID:
                return
            player = BigWorld.player()
            player.inputHandler.onVehicleShaken(self, center, dir, vehicles.g_cache.shotEffects[effectsIndex]['caliber'], ShakeReason.SPLASH)
            if attackerID == BigWorld.player().playerVehicleID:
                marker = getattr(self, 'marker', None)
                if marker is not None:
                    manager = g_windowsManager.battleWindow.vMarkersManager
                    manager.updateMarkerState(marker, 'hit_pierced')

    def showVehicleCollisionEffect(self, pos):
        if not self.isStarted:
            return
        self.showCollisionEffect(pos)
        self.appearance.executeRammingVibrations()

    def showCollisionEffect(self, hitPos, collisionEffectName = 'collisionVehicle', collisionNormal = None):
        hullAppearance = self.appearance.modelsDesc['hull']
        invWorldMatrix = Math.Matrix(hullAppearance['model'].matrix)
        invWorldMatrix.invert()
        rot = Math.Matrix()
        if collisionNormal is None:
            rot.setRotateYPR((random.uniform(-3.14, 3.14), random.uniform(-1.5, 1.5), 0.0))
        else:
            rot.setRotateYPR((0, 0, 0))
        mat = Math.Matrix()
        mat.setTranslate(hitPos)
        mat.preMultiply(rot)
        mat.postMultiply(invWorldMatrix)
        effectsList = self.typeDescriptor.type.effects.get(collisionEffectName, [])
        if effectsList:
            stages, effects, _ = random.choice(effectsList)
            hullAppearance['boundEffects'].addNew(mat, effects, stages, entity=self, surfaceNormal=collisionNormal)

    def set_damageStickers(self, prev = None):
        if self.isStarted:
            prev = self.__prevDamageStickers
            curr = frozenset(self.damageStickers)
            self.__prevDamageStickers = curr
            for sticker in prev.difference(curr):
                self.appearance.removeDamageSticker(sticker)

            descr = self.typeDescriptor
            for sticker in curr.difference(prev):
                self.appearance.addDamageSticker(sticker, *_decodeSegment(descr, sticker))

    def set_publicStateModifiers(self, prev = None):
        if self.isStarted:
            prev = self.__prevPublicStateModifiers
            curr = frozenset(self.publicStateModifiers)
            self.__prevPublicStateModifiers = curr
            self.__updateModifiers(curr.difference(prev), prev.difference(curr))

    def set_engineMode(self, prev):
        if self.isStarted:
            self.appearance.changeEngineMode(self.engineMode, True)

    def set_isStrafing(self, prev):
        if isinstance(self.filter, BigWorld.WGVehicleFilter):
            self.filter.isStrafing = self.isStrafing

    def set_gunAnglesPacked(self, prev):
        if isinstance(self.filter, BigWorld.WGVehicleFilter):
            yaw, pitch = decodeGunAngles(self.gunAnglesPacked, self.typeDescriptor.gun['pitchLimits']['absolute'])
            self.filter.syncGunAngles(yaw, pitch)

    def set_health(self, prev):
        if self.health > 0 and prev <= 0:
            self.health = prev

    def set_isCrewActive(self, prev):
        if self.isStarted:
            self.appearance.onVehicleHealthChanged()
            if not self.isPlayer:
                marker = getattr(self, 'marker', None)
                if marker is not None:
                    g_windowsManager.battleWindow.vMarkersManager.onVehicleHealthChanged(marker, self.health)
            if not self.isCrewActive and self.health > 0:
                self.__onVehicleDeath()

    def onHealthChanged(self, newHealth, attackerID, attackReasonID):
        if newHealth > 0 and self.health <= 0:
            return
        elif not self.isStarted:
            return
        else:
            # Update Tank Health
            damage = self.__tankHealth[self.__battleID] - newHealth
            self.__tankHealth[self.__battleID] = newHealth

            if attackReasonID == 0:
                self.damage_announce(attackerID, damage)

            if not self.isPlayer:
                marker = getattr(self, 'marker', None)
                if marker is not None:
                    g_windowsManager.battleWindow.vMarkersManager.onVehicleHealthChanged(marker, newHealth, attackerID, attackReasonID)
            self.appearance.onVehicleHealthChanged()
            if self.health <= 0 and self.isCrewActive:
                self.__onVehicleDeath()

    def damage_announce(self, attackerID, damage):
        try:
            # Get Attacker
            p = BigWorld.player()
            current = p.arena.vehicles.get(self.__battleID)
            attacker = p.arena.vehicles.get(attackerID)

            # Update attackers health if they have not been seen
            if attackerID not in self.__tankHealth:
                self.__tankHealth[attackerID] = attacker["vehicleType"].maxHealth

            def isOptionalEquipped(vehicle, optional_name):
                for item in vehicle.optionalDevices:
                    if item is not None and optional_name in item.name:
                        return True
                return False

            def calculateReload(vehicle):
                loader_skill = 126.5
                if isOptionalEquipped(attacker["vehicleType"], "improvedVentilation"):
                    loader_skill = 132.0

                other_bonus = 1.0
                # Take into account adrenaline skill
                if self.__tankHealth[attackerID] < attacker["vehicleType"].maxHealth * 0.1:
                    other_bonus *= 0.909

                # Take into account rammer
                if isOptionalEquipped(attacker["vehicleType"], "TankRammer"):
                    other_bonus *= 0.9

                return ((attacker["vehicleType"].gun["reloadTime"] * 0.875) / (0.00375 * loader_skill + 0.5)) * other_bonus

            def getShellPrice(nationID, shellID):
                import ResMgr, nations
                from items import _xml, vehicles
                from constants import ITEM_DEFS_PATH

                price = {}
                xmlPath = ITEM_DEFS_PATH + 'vehicles/' + nations.NAMES[nationID] + '/components/shells.xml'
                for name, subsection in ResMgr.openSection(xmlPath).items():
                    if name != 'icons':
                        xmlCtx = (None, xmlPath + '/' + name)
                        if _xml.readInt(xmlCtx, subsection, 'id', 0, 65535) == shellID:
                            price = _xml.readPrice(xmlCtx, subsection, 'price')
                            break
                ResMgr.purge(xmlPath, True)

                return price

            def formatMessage(inMessage, defenderID, attackerID):
                # Get Information
                p = BigWorld.player()
                current = p.arena.vehicles.get(defenderID)
                attacker = p.arena.vehicles.get(attackerID)

                # Do if statements
                for shell in attacker["vehicleType"].gun["shots"]:
                    if self.__hitType == shell["shell"]["effectsIndex"]:
                        if inMessage.find("{{if_shell_gold}}") != -1:
                            start = inMessage.find("{{if_shell_gold}}")
                            end = inMessage.find("{{endif}}")
                            if end != -1:
                                price = getShellPrice(shell["shell"]["id"][0], shell["shell"]["id"][1])
                                if price[1] == 0:
                                    inMessage = inMessage[:start] + inMessage[end+9:]
                                else:
                                    inMessage = inMessage[:start] + inMessage[start+17:end] + inMessage[end+9:]
                        break

                # Create starting message
                message = inMessage

                # Parse Commands
                position = 0
                while position < len(inMessage):
                    commandStart = inMessage[position:].find("{{")
                    commandEnd = inMessage[position:].find("}}")

                    if commandStart is -1:
                        break

                    width = -1
                    command = inMessage[position+commandStart+2:position+commandEnd]

                    if command.find("c:") is -1:
                        seperator = command.find(":")
                        if seperator is not -1:
                            width = int(command[seperator+1:])
                            command = command[:seperator]

                    result = ""
                    if command == "defender_user":
                        result = current["name"]
                    elif command == "defender_tier":
                        result = str(current["vehicleType"].level)
                    elif command == "defender_tank_long":
                        result = unicode(current["vehicleType"].type.userString, 'utf-8')
                    elif command == "defender_tank_short":
                        result = unicode(current["vehicleType"].type.shortUserString, 'utf-8')
                    elif command == "defender_cur_health":
                        result = str(self.__tankHealth[defenderID])
                    elif command == "defender_max_health":
                        result = str(current["vehicleType"].maxHealth)
                    elif command == "user":
                        result = attacker["name"]
                    elif command == "tier":
                        result = str(attacker["vehicleType"].level)
                    elif command == "tank_long":
                        result = unicode(attacker["vehicleType"].type.userString, 'utf-8')
                    elif command == "tank_short":
                        result = unicode(attacker["vehicleType"].type.shortUserString, 'utf-8')
                    elif command == "damage":
                        result = str(damage)
                    elif command == "cur_health":
                        result = str(self.__tankHealth[attackerID])
                    elif command == "max_health":
                        result = str(attacker["vehicleType"].maxHealth)
                    elif command == "reload":
                        result = "{0:.2f}".format(calculateReload(attacker["vehicleType"])) + "s"
                    elif command == "damage_roll":
                        for shell in attacker["vehicleType"].gun["shots"]:
                            if self.__hitType == shell["shell"]["effectsIndex"]:
                                percent = ((damage - shell["shell"]["damage"][0]) / shell["shell"]["damage"][0]) * 100
                                percent = min(25.0, max(percent, -25.0))
                                result = "{0:.2f}".format(percent) + "%"
                                break
                    elif command == "shell_type":
                        for shell in attacker["vehicleType"].gun["shots"]:
                            if self.__hitType == shell["shell"]["effectsIndex"]:
                                if shell["shell"]["kind"] == "ARMOR_PIERCING":
                                    result = self.__damageCfg["name"]["shell"]["ap"]
                                if shell["shell"]["kind"] == "ARMOR_PIERCING_CR":
                                    result = self.__damageCfg["name"]["shell"]["apcr"]
                                if shell["shell"]["kind"] == "HIGH_EXPLOSIVE":
                                    result = self.__damageCfg["name"]["shell"]["he"]
                                if shell["shell"]["kind"] == "HOLLOW_CHARGE":
                                    result = self.__damageCfg["name"]["shell"]["heat"]
                                break
                    elif command == "c:shell":
                        for shell in attacker["vehicleType"].gun["shots"]:
                            if self.__hitType == shell["shell"]["effectsIndex"]:
                                if shell["shell"]["kind"] == "ARMOR_PIERCING":
                                    result = self.__damageCfg["color"]["shell"]["ap"]
                                if shell["shell"]["kind"] == "ARMOR_PIERCING_CR":
                                    result = self.__damageCfg["color"]["shell"]["apcr"]
                                if shell["shell"]["kind"] == "HIGH_EXPLOSIVE":
                                    result = self.__damageCfg["color"]["shell"]["he"]
                                if shell["shell"]["kind"] == "HOLLOW_CHARGE":
                                    result = self.__damageCfg["color"]["shell"]["heat"]
                                break
                    elif command == "shot_delay":
                        if attacker["vehicleType"].gun["clip"][0] != 1:
                            result = "{0:.2f}".format(attacker["vehicleType"].gun["clip"][1]) + "s"
                        else:
                            result = "{0:.2f}".format(calculateReload(attacker["vehicleType"])) + "s"
                    elif command == "clip_size":
                        result = str(attacker["vehicleType"].gun["clip"][0])
                    elif command == "clip_delay":
                        result = "{0:.2f}".format(attacker["vehicleType"].gun["clip"][1]) + "s"
                    elif command == "burst_size":
                        result = str(attacker["vehicleType"].gun["burst"][0])
                    elif command == "burst_delay":
                        result = "{0:.2f}".format(attacker["vehicleType"].gun["burst"][1]) + "s"
                    else:
                        raise Exception("Invalid Command: %s" % (command))

                    # Replace Command
                    if width is -1:
                        message = message.replace("{{"+command+"}}", result)
                    else:
                        if len(result) < width:
                            result = " " * (width - len(result)) + result
                        message = message.replace("{{"+command+":"+str(width)+"}}", result[:width])

                    position += commandEnd+2

                return message

            if self.__damageCfg is not None:
                if self.__damageCfg["debug"]:
                    LOG_NOTE("Hit:", attackerID, attacker, attacker["vehicleType"].__dict__)

                currentVehicleID = p.playerVehicleID
                if hasattr(BigWorld.player().inputHandler.ctrl, 'curVehicleID') and self.__damageCfg["hit_message"]["spectator"]:
                    vehicleID = BigWorld.player().inputHandler.ctrl.curVehicleID
                    if vehicleID is not None:
                        currentVehicleID = vehicleID

                # Test if we are the attacker
                if currentVehicleID == attackerID:
                    if self.__damageCfg["hit_message"]["given"]["enabled"]:
                        if damage == 0:
                            if self.__damageCfg["hit_message"]["given"]["bounce"]["enabled"]:
                                MessengerEntry.g_instance.gui.addClientMessage(formatMessage(self.__damageCfg["hit_message"]["given"]["bounce"]["format"], self.__battleID, attackerID))
                        else:
                            MessengerEntry.g_instance.gui.addClientMessage(formatMessage(self.__damageCfg["hit_message"]["given"]["format"], self.__battleID, attackerID))
                elif self.__battleID == currentVehicleID:
                    if p.team != attacker["team"]:
                        if self.__damageCfg["hit_message"]["received"]["enabled"]:
                            if damage == 0:
                                if self.__damageCfg["hit_message"]["received"]["bounce"]["enabled"]:
                                    MessengerEntry.g_instance.gui.addClientMessage(formatMessage(self.__damageCfg["hit_message"]["received"]["bounce"]["format"], self.__battleID, attackerID))
                            else:
                                MessengerEntry.g_instance.gui.addClientMessage(formatMessage(self.__damageCfg["hit_message"]["received"]["format"], self.__battleID, attackerID))
                    else:
                        if self.__damageCfg["team_announce"]["enabled"]:
                            if not BattleReplay.g_replayCtrl.isPlaying and damage > self.__damageCfg["team_announce"]["min_damage"]:
                                from ChatManager import chatManager

                                message = formatMessage(self.__damageCfg["team_announce"]["format"], self.__battleID, attackerID)
                                BigWorld.player().broadcast(chatManager.battleTeamChannelID, message.encode('ascii', 'xmlcharrefreplace'))
        except Exception, err:
            self.printError(str(err))

    def printError(self, message):
        LOG_NOTE("Damage Announcer Error: ", message)
        if self.__damageCfg["debug"]:
            MessengerEntry.g_instance.gui.addClientMessage("<font color=\"#FF0000\">Damage Announcer Error: " + message + "</font>")

    def onPushed(self, x, z):
        try:
            distSqr = BigWorld.player().position.distSqrTo(self.position)
            if distSqr > 1600.0:
                self.filter.setPosition(x, z)
        except:
            pass

    def showRammingEffect(self, energy, point):
        pass

    def onStaticCollision(self, energy, point, normal, miscFlags):
        if not self.isStarted:
            return
        else:
            self.appearance.stopSwinging()
            BigWorld.player().inputHandler.onVehicleCollision(self, self.getSpeed())
            isTrackCollision = bool(miscFlags & 1)
            isSptCollision = bool(miscFlags >> 1 & 1)
            isSptDestroyed = bool(miscFlags >> 2 & 1)
            hitPt = point
            surfNormal = normal
            if not isSptCollision:
                segStart = point - normal * 3.0
                segStop = point + normal * 2.0
                matInfo = BigWorld.wg_getMatInfoNearPoint(self.spaceID, segStart, segStop, point, self.__isDestructibleBroken)
                matKind = 0
                if matInfo is None:
                    effectIdx = EFFECT_MATERIAL_INDEXES_BY_NAMES['ground']
                    hitPt = point
                    surfNormal = normal
                else:
                    hitPt, surfNormal, chunkID, itemIndex, matKind, fname = matInfo
                    effectIdx = None
                    if matKind >= DESTRUCTIBLE_MATKIND.MIN and matKind <= DESTRUCTIBLE_MATKIND.MIN:
                        desc = AreaDestructibles.g_cache.getDescByFilename(fname)
                        if desc is not None:
                            type = desc['type']
                            if type == DESTR_TYPE_STRUCTURE:
                                moduleDesc = desc['modules'].get(matKind)
                                if moduleDesc is not None:
                                    effectIdx = moduleDesc.get('effectMtrlIdx')
                    else:
                        effectIdx = helpers.calcEffectMaterialIndex(matKind)
                    if effectIdx is None:
                        effectIdx = EFFECT_MATERIAL_INDEXES_BY_NAMES['ground']
            else:
                if isSptDestroyed:
                    return
                hitPt = point
                matKind = SPT_MATKIND.SOLID
                effectIdx = EFFECT_MATERIAL_INDEXES_BY_NAMES['wood']
            self.__showStaticCollisionEffect(energy, matKind, effectIdx, hitPt, surfNormal, isTrackCollision)

    def getComponents(self):
        res = []
        vehicleDescr = self.typeDescriptor
        m = Math.Matrix()
        m.setIdentity()
        res.append((vehicleDescr.chassis, m))
        hullOffset = vehicleDescr.chassis['hullPosition']
        m = Math.Matrix()
        m.setTranslate(-hullOffset)
        res.append((vehicleDescr.hull, m))
        turretYaw = Math.Matrix(self.appearance.turretMatrix).yaw
        turretMatrix = Math.Matrix()
        turretMatrix.setTranslate(-hullOffset - vehicleDescr.hull['turretPositions'][0])
        m = Math.Matrix()
        m.setRotateY(-turretYaw)
        turretMatrix.postMultiply(m)
        res.append((vehicleDescr.turret, turretMatrix))
        gunPitch = Math.Matrix(self.appearance.gunMatrix).pitch
        gunMatrix = Math.Matrix()
        gunMatrix.setTranslate(-vehicleDescr.turret['gunPosition'])
        m = Math.Matrix()
        m.setRotateX(-gunPitch)
        gunMatrix.postMultiply(m)
        gunMatrix.preMultiply(turretMatrix)
        res.append((vehicleDescr.gun, gunMatrix))
        return res

    def collideSegment(self, startPoint, endPoint, skipGun = False):
        if not segmentMayHitVehicle(self.typeDescriptor, startPoint, endPoint, self.position):
            return

        worldToVehMatrix = Math.Matrix(self.model.matrix)
        worldToVehMatrix.invert()
        startPoint = worldToVehMatrix.applyPoint(startPoint)
        endPoint = worldToVehMatrix.applyPoint(endPoint)
        res = None
        for compDescr, compMatrix in self.getComponents():
            if skipGun and compDescr.get('itemTypeName') == 'vehicleGun':
                continue
            collisions = compDescr['hitTester'].localHitTest(compMatrix.applyPoint(startPoint), compMatrix.applyPoint(endPoint))
            if collisions is None:
                continue
            for dist, _, hitAngleCos, matKind in collisions:
                if res is None or res[0] >= dist:
                    matInfo = compDescr['materials'].get(matKind)
                    res = (dist, hitAngleCos, matInfo.armor if matInfo is not None else 0)
        return res

    def isAlive(self):
        return self.isCrewActive and self.health > 0

    def getSpeed(self):
        return self.filter.speedInfo.value[0]

    def startVisual(self):
        assert not self.isStarted

        avatar = BigWorld.player()

        self.appearance.start(self, self.__prereqs)
        self.__prereqs = None
        self.appearance.changeEngineMode(self.engineMode)
        self.appearance.onVehicleHealthChanged()
        if self.isPlayer:
            BigWorld.wgAddEdgeDetectEntity(self, 0, True)
            self.appearance.turretMatrix.target = avatar.gunRotator.turretMatrix
            self.appearance.gunMatrix.target = avatar.gunRotator.gunMatrix
            self.filter.allowStrafeCompensation = False
        else:
            self.marker = g_windowsManager.battleWindow.vMarkersManager.createMarker(self.proxy)
            self.filter.allowStrafeCompensation = True
        self.isStarted = True
        self.set_publicStateModifiers()
        self.set_damageStickers()
        if not self.isAlive():
            self.__onVehicleDeath(True)
        minimap = g_windowsManager.battleWindow.minimap
        minimap.notifyVehicleStart(self.id)
        self.__startWGPhysics()
        if self.isPlayer:
            nationId = self.typeDescriptor.type.id[0]
            SoundGroups.g_instance.soundModes.setCurrentNation(nations.NAMES[nationId])

        if avatar.name == self.publicInfo.name:
            if self.__damageCfg is None and self.__hasWarned == False:
                self.__hasWarned = True
                MessengerEntry.g_instance.gui.addClientMessage("<font color=\"#FF0000\">Damage Announcer configuration file missing.</font>")

    def stopVisual(self):
        assert self.isStarted
        if self.isPlayer:
            BigWorld.wgDelEdgeDetectEntity(self)
        self.__stopExtras()
        if hasattr(self, 'marker'):
            manager = g_windowsManager.battleWindow.vMarkersManager
            manager.destroyMarker(self.marker)
            self.marker = -1
        self.appearance.destroy()
        self.appearance = None
        self.isStarted = False
        minimap = g_windowsManager.battleWindow.minimap
        minimap.notifyVehicleStop(self.id)
        self.__stopWGPhysics()

    def showPlayerMovementCommand(self, flags):
        if not self.isStarted:
            return
        powerMode = self.engineMode[0]
        if flags == 0 and powerMode != 0:
            self.appearance.changeEngineMode((1, 0))
            return
        if flags != 0 and powerMode != 0:
            self.appearance.changeEngineMode((3, flags))
            return

    def _isDestructibleMayBeBroken(self, chunkID, itemIndex, matKind, itemFilename, itemScale, vehSpeed):
        desc = AreaDestructibles.g_cache.getDescByFilename(itemFilename)
        if desc is None:
            return False
        ctrl = AreaDestructibles.g_destructiblesManager.getController(chunkID)
        if ctrl is None:
            return False
        if ctrl.isDestructibleBroken(itemIndex, matKind, desc['type']):
            return True
        mass = self.typeDescriptor.physics['weight']
        instantDamage = 0.5 * mass * vehSpeed * vehSpeed * 0.00015
        if desc['type'] == DestructiblesCache.DESTR_TYPE_STRUCTURE:
            moduleDesc = desc['modules'].get(matKind)
            if moduleDesc is None:
                return False
            refHealth = moduleDesc['health']
        else:
            unitMass = AreaDestructibles.g_cache.unitVehicleMass
            instantDamage *= math.pow(mass / unitMass, desc['kineticDamageCorrection'])
            refHealth = desc['health']
        return DestructiblesCache.scaledDestructibleHealth(itemScale, refHealth) < instantDamage

    def __isDestructibleBroken(self, chunkID, itemIndex, matKind, itemFilename):
        desc = AreaDestructibles.g_cache.getDescByFilename(itemFilename)
        if desc is None:
            return False
        ctrl = AreaDestructibles.g_destructiblesManager.getController(chunkID)
        if ctrl is None:
            return False
        return ctrl.isDestructibleBroken(itemIndex, matKind, desc['type'])

    def __showStaticCollisionEffect(self, energy, matKind, effectIdx, hitPoint, normal, isTrackCollision):
        heavyVelocities = self.typeDescriptor.type.heavyCollisionEffectVelocities
        heavyEnergy = heavyVelocities['track'] if isTrackCollision else heavyVelocities['hull']
        heavyEnergy = 0.5 * heavyEnergy * heavyEnergy
        postfix = '%sCollisionLight' if energy < heavyEnergy else '%sCollisionHeavy'
        effectName = ''
        if effectIdx < len(EFFECT_MATERIALS):
            effectName = EFFECT_MATERIALS[effectIdx]
        effectName = postfix % effectName
        if effectName in self.typeDescriptor.type.effects:
            self.showCollisionEffect(hitPoint, effectName, normal)
        if self.isPlayer:
            self.appearance.executeRammingVibrations(matKind)

    def __startWGPhysics(self):
        typeDescr = self.typeDescriptor
        self.wgPhysics = BigWorld.WGVehiclePhysics()
        physics = self.wgPhysics
        physics_shared.initVehiclePhysics(physics, typeDescr)
        arenaMinBound, arenaMaxBound = (-10000, -10000), (10000, 10000)
        physics.setArenaBounds(arenaMinBound, arenaMaxBound)
        physics.enginePower = typeDescr.physics['enginePower'] / 1000.0
        physics.owner = weakref.ref(self)
        physics.staticMode = False
        physics.movementSignals = 0
        physics.damageDestructibleCb = None
        physics.destructibleHealthRequestCb = None
        self.filter.setVehiclePhysics(physics)
        player = BigWorld.player()
        physics.visibilityMask = ArenaType.getVisibilityMask(player.arenaTypeID >> 16)
        yaw, pitch = decodeGunAngles(self.gunAnglesPacked, typeDescr.gun['pitchLimits']['absolute'])
        self.filter.syncGunAngles(yaw, pitch)
        self.appearance.fashion.placingCompensationMatrix = self.filter.placingCompensationMatrix

    def __stopWGPhysics(self):
        self.wgPhysics.damageDestructibleCb = None
        self.wgPhysics.destructibleHealthRequestCb = None
        self.wgPhysics = None

    def __stopExtras(self):
        extraTypes = self.typeDescriptor.extras
        for index, data in self.extras.items():
            extraTypes[index].stop(data)

        if self.extras:
            LOG_CODEPOINT_WARNING()

    def __updateModifiers(self, addedExtras, removedExtras):
        descr = self.typeDescriptor
        for idx in removedExtras:
            data = self.extras.get(idx)
            if data is not None:
                data['extra'].stop(data)
            else:
                LOG_WARNING('Attempt to remove non-existent EntityExtra data', self.typeDescriptor.name, self.typeDescriptor.extras[idx].name)

        for idx in addedExtras:
            if idx < 0 or idx >= len(self.typeDescriptor.extras):
                LOG_WARNING('Attempt to add unknown EntityExtra', self.typeDescriptor.name, idx)
            else:
                try:
                    self.typeDescriptor.extras[idx].startFor(self)
                except Exception:
                    LOG_CURRENT_EXCEPTION()

    def __onVehicleDeath(self, isDeadStarted = False):
        if not self.isPlayer:
            marker = getattr(self, 'marker', None)
            if marker is not None:
                manager = g_windowsManager.battleWindow.vMarkersManager
                manager.updateMarkerState(marker, 'dead', isDeadStarted)
        self.stopHornSound(True)
        TriggersManager.g_manager.fireTrigger(TRIGGER_TYPE.VEHICLE_DESTROYED, vehicleId=self.id)

    def playHornSound(self, hornID):
        if not self.isStarted:
            return
        else:
            hornDesc = vehicles.g_cache.horns().get(hornID)
            if hornDesc is None:
                return
            self.stopHornSound(True)
            self.__hornSounds = []
            self.__hornMode = hornDesc['mode']
            model = self.appearance.modelsDesc['turret']['model']
            for sndEventId in hornDesc['sounds']:
                snd = model.getSound(sndEventId)
                snd.volume *= self.typeDescriptor.type.hornVolumeFactor
                self.__hornSounds.append(snd)

            if self.__hornSounds[0] is not None:
                self.__hornSounds[0].play()
                if self.__hornMode == 'continuous' and hornDesc['maxDuration'] > 0:
                    self.__stopHornSoundCallback = BigWorld.callback(hornDesc['maxDuration'], self.stopHornSound)

    def stopHornSound(self, forceSilence = False):
        if not forceSilence and self.__hornMode == 'twoSounds':
            if self.__hornSounds[1] is not None:
                self.__hornSounds[1].play()
        else:
            for snd in self.__hornSounds:
                if snd is not None:
                    snd.stop()

            self.__hornSounds = (None,)
        if self.__stopHornSoundCallback is not None:
            BigWorld.cancelCallback(self.__stopHornSoundCallback)
            self.__stopHornSoundCallback = None
        self.__hornMode = ''

    def isHornActive(self):
        if self.__hornMode == 'twoSounds':
            return True

        anySoundPlaying = False
        for snd in self.__hornSounds:
            if snd is not None:
                state = snd.state
                if state is not None and state.find('playing') != -1:
                    return True

        return False


def _decodeSegment(vehicleDescr, segment):
    compIdx = int(segment & 65280) >> 8
    if compIdx == 0:
        componentName = 'chassis'
        bbox = vehicleDescr.chassis['hitTester'].bbox
    elif compIdx == 1:
        componentName = 'hull'
        bbox = vehicleDescr.hull['hitTester'].bbox
    elif compIdx == 2:
        componentName = 'turret'
        bbox = vehicleDescr.turret['hitTester'].bbox
    elif compIdx == 3:
        componentName = 'gun'
        bbox = vehicleDescr.gun['hitTester'].bbox
    else:
        LOG_CODEPOINT_WARNING(compIdx)
    min = Math.Vector3(bbox[0])
    delta = bbox[1] - min
    segStart = min + Math.Vector3(*(k * (segment >> shift & 255) / 255.0 for k, shift in izip(delta, xrange(16, 33, 8))))
    segEnd = min + Math.Vector3(*(k * (segment >> shift & 255) / 255.0 for k, shift in izip(delta, xrange(40, 57, 8))))
    dir = segEnd - segStart
    dir.normalise()
    segStart -= dir * 0.01
    segEnd += dir * 0.01
    return (componentName,
     segment & 255,
     segStart,
     segEnd)


def _stripVehCompDescrIfRoaming(vehCompDescr):
    if game_control.g_instance.roaming.isInRoaming():
        vehCompDescr = vehicles.stripCustomizationFromVehicleCompactDescr(vehCompDescr, True, True, False)[0]
    return vehCompDescr
