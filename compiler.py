import py_compile, zipfile

py_compile.compile("res_mods/0.8.8/scripts/client/vehicle.py")

fZip = zipfile.ZipFile( "TK Announce.zip", "w" )
fZip.write("res_mods/0.8.8/scripts/client/vehicle.pyc")
fZip.close()
