import py_compile, zipfile, os

WOTVersion = "0.9.0"

if os.path.exists("ReceivedDamage-EU.zip"):
	os.remove("ReceivedDamage-EU.zip")

py_compile.compile("src/vehicle.py")

fZip = zipfile.ZipFile( "ReceivedDamage-EU.zip", "w" )
fZip.write("src/vehicle.pyc", "res_mods/"+WOTVersion+"/scripts/client/vehicle.pyc")
fZip.write("data/vehicle_damage.json", "res_mods/"+WOTVersion+"/scripts/client/vehicle_damage.json")
fZip.close()
