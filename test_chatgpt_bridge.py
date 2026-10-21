import os, tempfile
from pathlib import Path
from io import BytesIO
from flask import Flask
from chatgpt_bridge import register_bridge

with tempfile.TemporaryDirectory() as d:
    app=Flask(__name__)
    register_bridge(app,lambda t: {"name":"Test"} if t=="testsite" else None,
                    Path(d),{"photos":"photos","docs":"docs"},{".jpg",".jpeg",".png"})
    c=app.test_client()
    url="/api/chatgpt/ingest/testsite"
    def send(name,content,site="testsite",token="test-secret"):
        return c.post(f"/api/chatgpt/ingest/{site}",
            headers={"Authorization":"Bearer "+token},
            data={"files":(BytesIO(content),name)},content_type="multipart/form-data")
    assert send("a.jpg",b"photo-test").status_code==401
    os.environ["GENBA_BRIDGE_TOKEN"]="test-secret"
    assert send("a.jpg",b"photo-test",token="bad").status_code==401
    assert send("a.jpg",b"photo-test",site="unknown").status_code==404
    a=send("a.jpg",b"photo-test")
    assert a.status_code==200 and a.json["uploaded"]==1,a.json
    b=send("a.jpg",b"photo-test")
    assert b.status_code==200 and b.json["uploaded"]==0 and b.json["results"][0]["status"]=="duplicate",b.json
    pdf=send("survey.pdf",b"%PDF-1.4\nexample")
    assert pdf.json["uploaded"]==1,pdf.json
    assert (Path(d)/"testsite"/"docs").exists()
    invalid=send("bad.exe",b"hello")
    assert invalid.json["uploaded"]==0 and invalid.json["results"][0]["status"]=="unsupported"
    print("PASS auth, unknown site, photo, duplicate, PDF, blocked extension")
