from whatshap.core import Read

def test_read():
	r = Read("name", 15)
	assert r.getName() == "name"
	assert r.getMapq() == 15