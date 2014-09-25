import py_compile, zipfile, os, subprocess
import shlex

WOTVersion = "0.9.3"
ZIPName = "ReceivedDamage-NA.zip"

if os.path.exists( ZIPName ):
	os.remove( ZIPName )

p = subprocess.Popen(shlex.split("pyobfuscate -a src/vehicle.py"), stdout=subprocess.PIPE)
(output, err) = p.communicate()

with open("src/vehicle_obs.py", "w") as text_file:
	text_file.write(output)

py_compile.compile("src/vehicle_obs.py")

fZip = zipfile.ZipFile( ZIPName, "w" )
fZip.write("src/vehicle_obs.pyc", "res_mods/"+WOTVersion+"/scripts/client/vehicle.pyc")
fZip.write("data/vehicle_damage.json", "res_mods/"+WOTVersion+"/scripts/client/vehicle_damage.json")
fZip.close()

if os.path.exists( "src/vehicle_obs.py" ):
	os.remove( "src/vehicle_obs.py" )
