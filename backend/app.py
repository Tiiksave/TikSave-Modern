from flask import Flask,request,jsonify,send_from_directory,send_file
import yt_dlp,os,uuid,threading,glob

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND=os.path.join(BASE,"frontend")
DOWNLOADS=os.path.join(BASE,"downloads")
os.makedirs(DOWNLOADS,exist_ok=True)

app=Flask(__name__)
jobs={}
lock=threading.Lock()

def ffmpeg():
 p=r"C:\Users\aqeel\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin"
 return p if os.path.isfile(os.path.join(p,"ffmpeg.exe")) else "ffmpeg"

def hook(jid,d):
 with lock:
  j=jobs.get(jid)
  if not j:return
  if d.get("status")=="downloading":
   total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0
   done=d.get("downloaded_bytes") or 0
   j["progress"]=round(done*100/total,1) if total else 0
  elif d.get("status")=="finished":
   j["progress"]=100
   j["status"]="processing"

def worker(jid,url,quality):
 try:
  with lock:jobs[jid]["status"]="preparing"
  prefix=os.path.join(DOWNLOADS,jid)
  opts={
   "outtmpl":prefix+".%(ext)s",
   "noplaylist":True,
   "quiet":True,
   "no_warnings":True,
   "retries":5,
   "fragment_retries":5,
   "socket_timeout":60,
   "progress_hooks":[lambda d:hook(jid,d)],
   "ffmpeg_location":ffmpeg(),
  }

  if quality=="best":
   opts["format"]="bestvideo+bestaudio/best"
  else:
   q=int(quality)
   opts["format"]=f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
  opts["merge_output_format"]="mp4"

  with yt_dlp.YoutubeDL(opts) as y:
   info=y.extract_info(url,download=True)

  files=[x for x in glob.glob(prefix+".*") if os.path.isfile(x) and not x.endswith(".part")]
  if not files:raise Exception("لم يتم إنشاء ملف الفيديو")

  path=max(files,key=os.path.getmtime)
  with lock:
   jobs[jid].update({
    "status":"done",
    "progress":100,
    "file":path,
    "filename":os.path.basename(path),
    "title":info.get("title","TiikSave")
   })
 except Exception as e:
  with lock:jobs[jid].update({"status":"error","error":str(e)[:1000]})

@app.get("/")
def home():
 return send_from_directory(FRONTEND,"index.html")

@app.get("/api/health")
def health():
 return jsonify({"status":"ok"})

@app.get("/api/download")
def download():
 url=request.args.get("url","").strip()
 quality=request.args.get("quality","best")
 if not url:return jsonify({"error":"الرابط مطلوب"}),400
 jid=uuid.uuid4().hex
 with lock:
  jobs[jid]={"id":jid,"status":"queued","progress":0,"error":None,"file":None}
 threading.Thread(target=worker,args=(jid,url,quality),daemon=True).start()
 return jsonify({"id":jid,"status":"queued"})

@app.get("/api/status")
def status():
 jid=request.args.get("id","")
 with lock:j=dict(jobs.get(jid,{}))
 if not j:return jsonify({"status":"error","error":"المهمة غير موجودة"}),404
 return jsonify(j)

@app.get("/api/file")
def file():
 jid=request.args.get("id","")
 with lock:j=jobs.get(jid)
 if not j or j.get("status")!="done":return jsonify({"error":"الملف غير جاهز"}),404
 path=j.get("file")
 if not path or not os.path.isfile(path):return jsonify({"error":"الملف غير موجود"}),404
 return send_file(path,as_attachment=True,download_name=j.get("filename","TiikSave.mp4"))

if __name__=="__main__":
 app.run(host="127.0.0.1",port=5000,threaded=True,debug=False)

