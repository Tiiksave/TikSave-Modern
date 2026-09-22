from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp, os, uuid, threading, time, re

BASE=os.path.dirname(os.path.abspath(__file__))
ROOT=os.path.dirname(BASE)
FRONT=os.path.join(ROOT,"frontend")
DOWNLOADS=os.path.join(BASE,"downloads")
os.makedirs(DOWNLOADS,exist_ok=True)

app=Flask(__name__,static_folder=FRONT,static_url_path="")
CORS(app)

jobs={}
lock=threading.Lock()

def clean_name(s):
    s=re.sub(r'[\\/:*?"<>|]+','_',s or 'TiikSave')
    return s[:150].strip() or 'TiikSave'

def fmt_bytes(n):
    if not n:return "—"
    n=float(n)
    for u in ["B","KB","MB","GB","TB"]:
        if n<1024:return f"{n:.1f} {u}"
        n/=1024
    return f"{n:.1f} PB"

def hook(job_id,d):
    with lock:
        j=jobs.get(job_id)
        if not j:return
        status=d.get("status")
        if status=="downloading":
            done=d.get("downloaded_bytes") or 0
            total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct=(done/total*100) if total else 0
            j.update({
                "status":"downloading",
                "percent":round(min(pct,100),1),
                "downloaded":done,
                "total":total,
                "speed":d.get("speed") or 0,
                "eta":d.get("eta"),
                "filename":d.get("filename","")
            })
        elif status=="finished":
            j["percent"]=100
            j["status"]="processing"

def download_worker(job_id,url,quality,audio=False):
    out=os.path.join(DOWNLOADS,job_id+"_%(title).80s.%(ext)s")
    try:
        with lock:
            jobs[job_id]["status"]="preparing"
        if audio:
            fmt="bestaudio/best"
        elif quality in ("1080","720","480","360"):
            fmt=f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        else:
            fmt="bestvideo+bestaudio/best"
        opts={
            "format":fmt,
            "outtmpl":out,
            "progress_hooks":[lambda d:hook(job_id,d)],
            "merge_output_format":"mp4",
            "noplaylist":True,
            "quiet":True,
            "no_warnings":True,
            "concurrent_fragment_downloads":4,
            "retries":5,
            "fragment_retries":5,
            "socket_timeout":30,
            "overwrites":True,
        }
        if audio:
            opts["postprocessors"]=[{
                "key":"FFmpegExtractAudio",
                "preferredcodec":"mp3",
                "preferredquality":"192"
            }]
        with yt_dlp.YoutubeDL(opts) as y:
            info=y.extract_info(url,download=True)
            title=info.get("title") or "TiikSave"
        files=[]
        for f in os.listdir(DOWNLOADS):
            if f.startswith(job_id+"_"):
                files.append(f)
        if not files:
            raise Exception("لم يتم العثور على الملف بعد التحميل")
        filename=max(files,key=lambda x:os.path.getmtime(os.path.join(DOWNLOADS,x)))
        path=os.path.join(DOWNLOADS,filename)
        with lock:
            jobs[job_id].update({
                "status":"ready",
                "percent":100,
                "downloaded":os.path.getsize(path),
                "total":os.path.getsize(path),
                "speed":0,
                "eta":0,
                "filename":filename,
                "title":title
            })
    except Exception as e:
        with lock:
            jobs[job_id].update({"status":"error","error":str(e)[:500]})

@app.route("/")
def home():
    return send_from_directory(FRONT,"index.html")

@app.route("/api/health")
def health():
    return jsonify({"ok":True})

@app.post("/api/info")
def info():
    data=request.get_json(silent=True) or {}
    url=(data.get("url") or "").strip()
    if not url:return jsonify({"error":"أدخل رابط الفيديو"}),400
    try:
        opts={"quiet":True,"no_warnings":True,"noplaylist":True,"skip_download":True}
        with yt_dlp.YoutubeDL(opts) as y:
            x=y.extract_info(url,download=False)
        heights=sorted(set(
            int(f["height"]) for f in (x.get("formats") or [])
            if f.get("height") and f.get("vcodec")!="none"
        ),reverse=True)
        qs=[str(h) for h in [1080,720,480,360] if h in heights]
        if not qs: qs=["best"]
        return jsonify({
            "ok":True,
            "title":x.get("title") or "فيديو",
            "thumbnail":x.get("thumbnail"),
            "duration":x.get("duration"),
            "uploader":x.get("uploader"),
            "qualities":qs
        })
    except Exception as e:
        return jsonify({"error":"تعذر الحصول على معلومات الفيديو: "+str(e)[:300]}),400

@app.get("/api/download")
def start_download():
    url=(request.args.get("url") or "").strip()
    quality=request.args.get("quality","best")
    audio=request.args.get("audio")=="1"
    if not url:return jsonify({"error":"الرابط مطلوب"}),400
    jid=uuid.uuid4().hex
    with lock:
        jobs[jid]={
            "status":"starting","percent":0,"downloaded":0,
            "total":0,"speed":0,"eta":None,"filename":"","title":""
        }
    threading.Thread(target=download_worker,args=(jid,url,quality,audio),daemon=True).start()
    return jsonify({"id":jid,"status":"starting"})

@app.get("/api/status")
def status():
    jid=request.args.get("id")
    with lock:
        j=dict(jobs.get(jid,{"status":"error","error":"المهمة غير موجودة"}))
    return jsonify(j)

@app.get("/api/file")
def file():
    jid=request.args.get("id")
    with lock:j=jobs.get(jid)
    if not j or j.get("status")!="ready":
        return "الملف غير جاهز",404
    return send_from_directory(DOWNLOADS,j["filename"],as_attachment=True)

if __name__=="__main__":
    app.run(host="127.0.0.1",port=int(os.environ.get("PORT",5000)),threaded=True)



