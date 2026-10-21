"""Opt-in, authenticated ChatGPT/MCP attachment ingestion for GENBA PHOTO.
Register with register_bridge(app, get_site_by_token, UPLOAD_BASE, PATHS, IMAGE_EXT).
Requires GENBA_BRIDGE_TOKEN environment variable; no token means endpoint disabled.
"""
import os, hmac, hashlib, json, sqlite3, uuid
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

def register_bridge(app, get_site_by_token, upload_base, paths, image_ext):
    bp=Blueprint("chatgpt_bridge", __name__)
    db_path=Path(upload_base)/"_chatgpt_bridge_uploads.sqlite3"
    def db():
        db_path.parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(str(db_path),timeout=30)
        con.execute("CREATE TABLE IF NOT EXISTS uploads (site TEXT NOT NULL, sha256 TEXT NOT NULL, filename TEXT NOT NULL, saved_as TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(site,sha256))")
        return con
    @bp.post("/api/chatgpt/ingest/<site_token>")
    def ingest(site_token):
        secret=os.environ.get("GENBA_BRIDGE_TOKEN","")
        auth=request.headers.get("Authorization","")
        if not secret or not hmac.compare_digest(auth,"Bearer "+secret):
            return jsonify(error="unauthorized"),401
        if not get_site_by_token(site_token):
            return jsonify(error="site_not_found"),404
        files=request.files.getlist("files")
        if not files:
            return jsonify(error="no_files"),400
        results=[]
        for f in files:
            name=Path(f.filename or "").name
            suffix=Path(name).suffix.lower()
            allowed={".pdf",".doc",".docx",".xls",".xlsx",".ppt",".pptx",".txt",".csv",".dwg",".dxf",".zip",".jpg",".jpeg",".png",".webp",".heic",".heif"}
            if suffix not in allowed:
                results.append({"name":name,"status":"unsupported"});continue
            data=f.stream.read(50*1024*1024+1)
            if len(data)>50*1024*1024:
                results.append({"name":name,"status":"too_large"});continue
            digest=hashlib.sha256(data).hexdigest()
            photo=suffix in image_ext
            folder=Path(upload_base)/site_token/paths["photos" if photo else "docs"]
            folder.mkdir(parents=True,exist_ok=True)
            with closing(db()) as con, con:
                found=con.execute("SELECT saved_as FROM uploads WHERE site=? AND sha256=?",(site_token,digest)).fetchone()
                if found:
                    results.append({"name":name,"status":"duplicate","saved_as":found[0]});continue
                target=folder/(datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_")+uuid.uuid4().hex[:12]+suffix)
                target.write_bytes(data)
                con.execute("INSERT INTO uploads VALUES (?,?,?,?,?)",(site_token,digest,name,str(target),datetime.now(timezone.utc).isoformat()))
                results.append({"name":name,"status":"uploaded","sha256":digest})
        return jsonify(results=results,uploaded=sum(x["status"]=="uploaded" for x in results)),200
    app.register_blueprint(bp)
