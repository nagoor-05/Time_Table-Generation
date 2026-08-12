from flask import Flask, request, jsonify, send_from_directory, send_file, session, abort
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from groq import Groq
import json, os, pathlib, openpyxl, random, secrets
from io import BytesIO
from legacy_scheduler import InputData, StudentGroup as LegacyStudentGroup, Teacher as LegacyTeacher, TimeTable as LegacyTimeTable, SchedulerMain as LegacyScheduler, parse_legacy_text, class_format, input_summary, slot_summary

BASE_DIR = pathlib.Path(__file__).parent
def find_frontend():
    for p in [BASE_DIR/'frontend', BASE_DIR.parent/'frontend', BASE_DIR]:
        if (p/'index.html').exists(): return p
    return BASE_DIR

FRONTEND_DIR = find_frontend()
app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path='')
CORS(app)
app.secret_key=os.environ.get('SECRET_KEY',secrets.token_hex(32))
mongo=MongoClient(os.environ.get('MONGODB_URI','mongodb://127.0.0.1:27017'),serverSelectionTimeoutMS=3000)
mongo_db=mongo[os.environ.get('MONGODB_DB','edu_schedule')]

class Expr:
    def __init__(self,fn):self.fn=fn
    def __call__(self,d):return self.fn(d)
    def __or__(self,o):return Expr(lambda d:self(d) or o(d))
    def __and__(self,o):return Expr(lambda d:self(d) and o(d))
class Field:
    def __init__(self,default=None):self.default=default;self.name=''
    def __set_name__(self,owner,name):self.name=name
    def __get__(self,obj,owner):
        if obj is None:return self
        return obj.__dict__.get(self.name,self.default() if callable(self.default) else self.default)
    def __set__(self,obj,value):obj.__dict__[self.name]=value
    def __eq__(self,v):return Expr(lambda d:d.get(self.name)==v)
    def __ne__(self,v):return Expr(lambda d:d.get(self.name)!=v)
    def in_(self,vals):vals=set(vals);return Expr(lambda d:d.get(self.name) in vals)
def _coerce(v):return int(v) if isinstance(v,str) and v.strip().isdigit() else v
class Query:
    def __init__(self,model,filters=None):self.model=model;self.filters=filters or []
    def filter_by(self,**kw):return Query(self.model,self.filters+[Expr(lambda d,kw=kw:all(d.get(k)==_coerce(v) for k,v in kw.items()))])
    def filter(self,*exprs):return Query(self.model,self.filters+list(exprs))
    def _docs(self):return [d for d in self.model._collection().find({}) if all(f(d) for f in self.filters)]
    def all(self):return [self.model._from_doc(d) for d in self._docs()]
    def first(self):
        docs=self._docs();return self.model._from_doc(docs[0]) if docs else None
    def get(self,ident):
        d=self.model._collection().find_one({'id':_coerce(ident)});return self.model._from_doc(d) if d else None
    def get_or_404(self,ident):
        obj=self.get(ident)
        if not obj:abort(404)
        return obj
    def count(self):return len(self._docs())
    def delete(self,**_):
        docs=self._docs();ids=[d['_id'] for d in docs];logical_ids={d.get('id') for d in docs}
        if ids:self.model._collection().delete_many({'_id':{'$in':ids}})
        for key in [k for k in db.session.loaded if k[0]==self.model.collection_name and k[1] in logical_ids]:db.session.loaded.pop(key,None)
        return len(ids)
    def update(self,values):
        docs=self._docs();ids=[d['_id'] for d in docs]
        if ids:self.model._collection().update_many({'_id':{'$in':ids}},{'$set':values})
        for d in docs:
            cached=db.session.loaded.get((self.model.collection_name,d.get('id')))
            if cached:
                for k,v in values.items():setattr(cached,k,v)
        return len(ids)
class QueryDescriptor:
    def __get__(self,obj,owner):return Query(owner)
class MongoModel:
    query=QueryDescriptor();fields=();defaults={}
    def __init__(self,**kw):
        for k in self.fields:setattr(self,k,kw.get(k,self.defaults.get(k)() if callable(self.defaults.get(k)) else self.defaults.get(k)))
    @classmethod
    def _collection(cls):return mongo_db[cls.collection_name]
    @classmethod
    def _from_doc(cls,d):
        if not d:return None
        o=cls(**{k:d.get(k) for k in cls.fields});o._id=d.get('_id');db.session.track(o);return o
    def _doc(self):return {k:getattr(self,k) for k in self.fields}
class SessionFacade:
    def __init__(self):self.loaded={}
    def track(self,o):self.loaded[(o.collection_name,o.id)]=o
    def add(self,o):
        if not getattr(o,'id',None):o.id=mongo_db.counters.find_one_and_update({'_id':o.collection_name},{'$inc':{'seq':1}},upsert=True,return_document=True)['seq']
        result=o._collection().insert_one(o._doc());o._id=result.inserted_id;self.track(o)
    def delete(self,o):o._collection().delete_one({'id':o.id});self.loaded.pop((o.collection_name,o.id),None)
    def flush(self):self.commit()
    def commit(self):
        for o in list(self.loaded.values()):o._collection().replace_one({'id':o.id},o._doc(),upsert=True)
        self.loaded.clear()
class DBFacade:session=SessionFacade()
db=DBFacade()

class Department(MongoModel):
    collection_name='departments';fields=('id','name','code');id=Field();name=Field('');code=Field('')
class Faculty(MongoModel):
    collection_name='faculty';fields=('id','name','faculty_id','dept_id','role','max_hours_per_week','preferred_slots');defaults={'role':'faculty','max_hours_per_week':18,'preferred_slots':'[]'}
    id=Field();name=Field('');faculty_id=Field('');dept_id=Field();role=Field('faculty');max_hours_per_week=Field(18);preferred_slots=Field('[]')
class Subject(MongoModel):
    collection_name='subjects';fields=('id','name','code','year','semester','dept_id','subject_type','hours_per_week','faculty_id','faculty_ids');defaults={'subject_type':'theory','hours_per_week':3,'faculty_ids':''}
    id=Field();name=Field('');code=Field('');year=Field();semester=Field();dept_id=Field();subject_type=Field('theory');hours_per_week=Field(3);faculty_id=Field();faculty_ids=Field('')
class Room(MongoModel):
    collection_name='rooms';fields=('id','name','room_type','capacity','dept_id');defaults={'room_type':'classroom','capacity':60}
    id=Field();name=Field('');room_type=Field('classroom');capacity=Field(60);dept_id=Field()
class TimetableEntry(MongoModel):
    collection_name='timetable_entries';fields=('id','dept_id','year','semester','subject_id','faculty_id','faculty_ids','room_id','day','slot_number','start_time','end_time','is_lab','created_by','last_modified','notes');defaults={'faculty_ids':'','is_lab':False,'created_by':'AI','last_modified':datetime.utcnow,'notes':''}
    id=Field();dept_id=Field();year=Field();semester=Field();subject_id=Field();faculty_id=Field();faculty_ids=Field('');room_id=Field();day=Field('');slot_number=Field();start_time=Field('');end_time=Field('');is_lab=Field(False);created_by=Field('AI');last_modified=Field(datetime.utcnow);notes=Field('')
    @property
    def subject(self):return Subject.query.get(self.subject_id) if self.subject_id else None
    @property
    def faculty(self):return Faculty.query.get(self.faculty_id) if self.faculty_id else None
    @property
    def room(self):return Room.query.get(self.room_id) if self.room_id else None
class User(MongoModel):
    collection_name='users';fields=('id','username','email','country','password_hash','created_at');defaults={'country':'','created_at':datetime.utcnow}
    id=Field();username=Field('');email=Field('');country=Field('');password_hash=Field('');created_at=Field(datetime.utcnow)
class StudentGroup(MongoModel):
    collection_name='student_groups';fields=('id','name','dept_id','year','semester','subjects','created_by');defaults={'subjects':list,'created_by':''}
    id=Field();name=Field('');dept_id=Field();year=Field();semester=Field();subjects=Field(list);created_by=Field('')
class LegacyScheduleRun(MongoModel):
    collection_name='legacy_schedule_runs';fields=('id','owner_id','source','configuration','input','fitness','collisions','history','timetables','created_at');defaults={'owner_id':None,'source':'form','configuration':dict,'input':dict,'fitness':0.0,'collisions':0,'history':list,'timetables':list,'created_at':datetime.utcnow}
    id=Field();owner_id=Field();source=Field('form');configuration=Field(dict);input=Field(dict);fitness=Field(0.0);collisions=Field(0);history=Field(list);timetables=Field(list);created_at=Field(datetime.utcnow)

DAYS  = ['Monday','Tuesday','Wednesday','Thursday','Friday']
SLOTS = [
    {'slot':1,'start':'08:30','end':'09:20'}, {'slot':2,'start':'09:20','end':'10:10'},
    {'slot':3,'start':'10:25','end':'11:15'}, {'slot':4,'start':'11:15','end':'12:05'},
    {'slot':5,'start':'13:10','end':'14:00'}, {'slot':6,'start':'14:00','end':'14:50'},
    {'slot':7,'start':'15:05','end':'15:55'}, {'slot':8,'start':'15:55','end':'16:45'},
]

def get_faculty_names(e):
    if e.faculty_ids:
        ids=[int(x) for x in e.faculty_ids.split(',') if x.strip().isdigit()]
        names=[Faculty.query.get(i).name for i in ids if Faculty.query.get(i)]
        return ', '.join(names) if names else (e.faculty.name if e.faculty else '')
    return e.faculty.name if e.faculty else ''

def entry_to_dict(e):
    return {'id':e.id,'dept_id':e.dept_id,'year':e.year,'semester':e.semester,
            'subject_id':e.subject_id,'subject_name':e.subject.name if e.subject else '',
            'subject_code':e.subject.code if e.subject else '',
            'faculty_id':e.faculty_id,'faculty_name':get_faculty_names(e),
            'faculty_ids':e.faculty_ids or '',
            'room_id':e.room_id,'room_name':e.room.name if e.room else '',
            'day':e.day,'slot_number':e.slot_number,'start_time':e.start_time,
            'end_time':e.end_time,'is_lab':e.is_lab,'created_by':e.created_by,
            'last_modified':e.last_modified.isoformat(),'notes':e.notes}

def check_conflicts(entries_data, exclude_id=None):
    existing = [e for e in TimetableEntry.query.all() if e.id != exclude_id]
    out = []
    for n in entries_data:
        for e in existing:
            if e.day==n['day'] and e.slot_number==n['slot_number']:
                if e.faculty_id==n.get('faculty_id'):
                    out.append(f"Faculty conflict {n['day']} slot {n['slot_number']}")
                if e.room_id and e.room_id==n.get('room_id'):
                    out.append(f"Room conflict {n['day']} slot {n['slot_number']}")
                if e.year==n.get('year') and e.dept_id==n.get('dept_id') and e.semester==n.get('semester'):
                    out.append(f"Group conflict {n['day']} slot {n['slot_number']}")
    return list(set(out))

@app.route('/api/departments', methods=['GET'])
def get_departments():
    return jsonify([{'id':d.id,'name':d.name,'code':d.code} for d in Department.query.all()])

@app.route('/api/departments', methods=['POST'])
def add_department():
    d=request.json; code=d['code'].upper().strip()
    ex=Department.query.filter_by(code=code).first()
    if ex: return jsonify({'id':ex.id,'name':ex.name,'code':ex.code})
    dept=Department(name=d['name'],code=code); db.session.add(dept); db.session.commit()
    return jsonify({'id':dept.id,'name':dept.name,'code':dept.code}), 201

@app.route('/api/faculty', methods=['GET'])
def get_faculty():
    q=Faculty.query
    if request.args.get('dept_id'): q=q.filter_by(dept_id=request.args['dept_id'])
    return jsonify([{'id':f.id,'name':f.name,'faculty_id':f.faculty_id,'dept_id':f.dept_id,
                     'role':f.role} for f in q.all()])

@app.route('/api/faculty', methods=['POST'])
def add_faculty():
    d=request.json; fid=d.get('faculty_id','').upper().strip()
    did=int(d.get('dept_id',0)) if d.get('dept_id') else None
    ex=Faculty.query.filter_by(faculty_id=fid,dept_id=did).first() if fid and did else None
    if ex: return jsonify({'id':ex.id,'name':ex.name,'faculty_id':ex.faculty_id})
    f=Faculty(**{k:d[k] for k in ['name','faculty_id','dept_id','role'] if k in d})
    if fid: f.faculty_id=fid
    db.session.add(f); db.session.commit()
    return jsonify({'id':f.id,'name':f.name,'faculty_id':f.faculty_id}), 201

@app.route('/api/faculty/<int:fid>', methods=['PUT','DELETE'])
def update_faculty(fid):
    f=Faculty.query.get_or_404(fid)
    if request.method=='DELETE':
        Subject.query.filter_by(faculty_id=fid).update({'faculty_id':None})
        db.session.delete(f); db.session.commit(); return jsonify({'deleted':fid})
    [setattr(f,k,v) for k,v in request.json.items()]
    db.session.commit(); return jsonify({'id':f.id,'name':f.name})

@app.route('/api/subjects', methods=['GET'])
def get_subjects():
    q=Subject.query
    for k in ['dept_id','semester','year']:
        if request.args.get(k): q=q.filter_by(**{k:int(request.args[k])})
    return jsonify([{'id':s.id,'name':s.name,'code':s.code,'year':s.year,'semester':s.semester,
                     'subject_type':s.subject_type,'hours_per_week':s.hours_per_week,
                     'faculty_id':s.faculty_id,'dept_id':s.dept_id,
                     'faculty_ids':s.faculty_ids or '',
                     'faculty_name':(
                         ', '.join([Faculty.query.get(int(i)).name for i in (s.faculty_ids or '').split(',')
                                    if i.strip().isdigit() and Faculty.query.get(int(i))])
                         if s.faculty_ids else
                         (Faculty.query.get(s.faculty_id).name if s.faculty_id else 'Unassigned')
                     )}
                    for s in q.all()])

@app.route('/api/subjects', methods=['POST'])
def add_subject():
    d=request.json; code=d.get('code','').strip(); did=d.get('dept_id')
    ex=Subject.query.filter_by(code=code,dept_id=did).first() if code and did else None
    if ex: return jsonify({'id':ex.id,'name':ex.name})
    s=Subject(**{k:d[k] for k in ['name','code','year','semester','dept_id','subject_type','hours_per_week','faculty_id'] if k in d})
    db.session.add(s); db.session.commit()
    return jsonify({'id':s.id,'name':s.name}), 201

@app.route('/api/subjects/<int:sid>', methods=['PUT','DELETE'])
def subject_detail(sid):
    s=Subject.query.get_or_404(sid)
    if request.method=='DELETE':
        db.session.delete(s); db.session.commit(); return jsonify({'deleted':sid})
    [setattr(s,k,v) for k,v in request.json.items()]
    db.session.commit(); return jsonify({'id':s.id})

@app.route('/api/rooms', methods=['GET'])
def get_rooms():
    return jsonify([{'id':r.id,'name':r.name,'room_type':r.room_type,'capacity':r.capacity,'dept_id':r.dept_id}
                    for r in Room.query.all()])

@app.route('/api/rooms', methods=['POST'])
def add_room():
    d=request.json; name=d.get('name','').strip(); did=d.get('dept_id')
    ex=Room.query.filter_by(name=name,dept_id=did).first() if name and did else None
    if ex: return jsonify({'id':ex.id,'name':ex.name})
    r=Room(**{k:d[k] for k in ['name','room_type','capacity','dept_id'] if k in d})
    db.session.add(r); db.session.commit()
    return jsonify({'id':r.id,'name':r.name}), 201

@app.route('/api/rooms/<int:rid>', methods=['DELETE'])
def delete_room(rid):
    r=Room.query.get_or_404(rid)
    db.session.delete(r); db.session.commit(); return jsonify({'deleted':rid})

@app.route('/api/timetable', methods=['GET'])
def get_timetable():
    q=TimetableEntry.query
    for k,t in [('dept_id',int),('year',int),('semester',int),('faculty_id',int),('room_id',int)]:
        if request.args.get(k): q=q.filter_by(**{k:t(request.args[k])})
    return jsonify([entry_to_dict(e) for e in q.all()])

@app.route('/api/timetable', methods=['POST'])
def add_timetable_entry():
    d=request.json; c=check_conflicts([d])
    if c: return jsonify({'error':'Conflict','conflicts':c}), 409
    e=TimetableEntry(**{k:d[k] for k in ['dept_id','year','semester','subject_id','faculty_id',
                        'room_id','day','slot_number','start_time','end_time','is_lab'] if k in d})
    e.created_by=d.get('created_by','HoD')
    db.session.add(e); db.session.commit()
    return jsonify(entry_to_dict(e)), 201

@app.route('/api/timetable/<int:eid>', methods=['PUT'])
def update_timetable_entry(eid):
    e=TimetableEntry.query.get_or_404(eid); d=request.json
    c=check_conflicts([{**entry_to_dict(e),**d}],exclude_id=eid)
    if c: return jsonify({'error':'Conflict','conflicts':c}), 409
    [setattr(e,k,v) for k,v in d.items() if hasattr(e,k)]
    e.last_modified=datetime.utcnow(); e.created_by=d.get('modified_by','HoD')
    db.session.commit(); return jsonify(entry_to_dict(e))

@app.route('/api/timetable/<int:eid>', methods=['DELETE'])
def delete_timetable_entry(eid):
    e=TimetableEntry.query.get_or_404(eid)
    db.session.delete(e); db.session.commit(); return jsonify({'deleted':eid})

@app.route('/api/timetable/bulk', methods=['DELETE'])
def clear_timetable():
    d=request.json; q=TimetableEntry.query
    if d.get('dept_id'): q=q.filter_by(dept_id=d['dept_id'])
    if d.get('semester'): q=q.filter_by(semester=int(d['semester']))
    if d.get('year'): q=q.filter_by(year=int(d['year']))
    n=q.count(); q.delete(); db.session.commit(); return jsonify({'deleted':n})

@app.route('/api/timetable/faculty/<int:fid>', methods=['GET'])
def get_faculty_timetable(fid):
    primary=TimetableEntry.query.filter_by(faculty_id=fid).all()
    multi=[e for e in TimetableEntry.query.filter(TimetableEntry.faculty_ids!='').all()
           if e.faculty_id!=fid and str(fid) in e.faculty_ids.split(',')]
    entries=primary+multi
    entries=list({e.id:e for e in entries}.values())
    by_day={d:sorted([entry_to_dict(e) for e in entries if e.day==d],key=lambda x:x['slot_number']) for d in DAYS}
    return jsonify({'faculty_id':fid,'schedule':by_day,'total_hours':len(entries)})

@app.route('/api/timetable/lab/<int:rid>', methods=['GET'])
def get_lab_timetable(rid):
    room=Room.query.get_or_404(rid); entries=TimetableEntry.query.filter_by(room_id=rid).all()
    by_day={d:[entry_to_dict(e) for e in entries if e.day==d] for d in DAYS}
    return jsonify({'room':{'id':room.id,'name':room.name,'type':room.room_type},'schedule':by_day})

@app.route('/api/conflicts', methods=['GET'])
def get_all_conflicts():
    entries=TimetableEntry.query
    if request.args.get('dept_id'): entries=entries.filter_by(dept_id=request.args['dept_id'])
    entries=entries.filter(TimetableEntry.created_by!='FILLER').all(); out=[]; seen=set()
    for i,e1 in enumerate(entries):
        for e2 in entries[i+1:]:
            if e1.day==e2.day and e1.slot_number==e2.slot_number:
                checks=[('Faculty',f"fac-{e1.faculty_id}-{e1.day}-{e1.slot_number}",
                          e1.faculty_id==e2.faculty_id, e1.faculty.name if e1.faculty else ''),
                        ('Room',f"room-{e1.room_id}-{e1.day}-{e1.slot_number}",
                          e1.room_id and e1.room_id==e2.room_id, e1.room.name if e1.room else '')]
                for ctype,key,cond,label in checks:
                    if cond and key not in seen:
                        out.append({'type':ctype,'day':e1.day,'slot':e1.slot_number,
                                    ctype.lower():label,'entries':[e1.id,e2.id]}); seen.add(key)
    return jsonify(out)

@app.route('/api/generate', methods=['POST'])
def generate_timetable():
    d=request.json; dept_id=d.get('dept_id'); year=d.get('year'); semester=d.get('semester')
    all_dept_subjects=Subject.query.filter_by(dept_id=dept_id,semester=semester,year=year).all()
    seen_codes={}; seen_names={}; dupes=[]
    for s in all_dept_subjects:
        code_key=s.code.strip().upper()
        name_key=s.name.strip().lower()
        if code_key in seen_codes or name_key in seen_names:
            dupes.append(s.id)
        else:
            seen_codes[code_key]=s.id
            seen_names[name_key]=s.id
    if dupes:
        Subject.query.filter(Subject.id.in_(dupes)).delete(synchronize_session=False)
        TimetableEntry.query.filter(TimetableEntry.subject_id.in_(dupes)).delete(synchronize_session=False)
        db.session.commit()
        print(f"ðŸ§¹ Removed {len(dupes)} duplicate subjects + their entries")
    subjects=Subject.query.filter_by(dept_id=dept_id,semester=semester,year=year).all()
    rooms=Room.query.filter_by(dept_id=dept_id).all()
    classrooms=[r for r in rooms if r.room_type=='classroom']
    labs=[r for r in rooms if r.room_type=='lab']
    if not subjects: return jsonify({'error':'No subjects found'}), 400
    if not rooms:    return jsonify({'error':'No rooms found'}), 400
    if not labs:     labs=classrooms

    th=[s for s in subjects if s.subject_type=='theory']
    prompt=(f"Timetable for dept={dept_id} yr={year} sem={semester}. "
            f"Theory: {[(s.id,s.name[:20],s.faculty_id,s.hours_per_week) for s in th]}. "
            f"Days Mon-Sat slots 1-8. Return ONLY JSON: "
            f'{{\"entries\":[{{\"subject_id\":0,\"faculty_id\":0,\"room_id\":{classrooms[0].id if classrooms else 1},'
            f'\"day\":\"Monday\",\"slot_number\":1,\"start_time\":\"08:30\",\"end_time\":\"09:20\",\"is_lab\":false}}]}}')
    raw='{"entries":[]}'
    for model in ['llama-3.3-70b-versatile','llama-3.1-70b-versatile','mixtral-8x7b-32768']:
        try:
            r=Groq(api_key=os.environ.get('GROQ_API_KEY')).chat.completions.create(
                model=model,messages=[{"role":"user","content":prompt}],max_tokens=3000,temperature=0.1)
            raw=r.choices[0].message.content.strip(); print(f"âœ… {model}"); break
        except Exception as e:
            if '429' in str(e) or 'rate' in str(e).lower(): continue
            return jsonify({'error':str(e)}), 500
    try:
        if '```' in raw: raw=raw.split('```')[1]; raw=raw[4:] if raw.startswith('json') else raw
    except: pass

    LAB_PAIRS={1:2,2:1,3:4,4:3,5:6,6:5,7:8,8:7}
    QUAD_BLOCKS=[(1,2,3,4),(5,6,7,8)]
    slot_map={s['slot']:s for s in SLOTS}
    occ_fac={}; occ_room={}; occ_grp={}; subj_days={}; day_lab_count={}

    existing=TimetableEntry.query.filter_by(dept_id=dept_id).filter(
        (TimetableEntry.year!=year)|(TimetableEntry.semester!=semester)).all()
    for e in existing:
        occ_room[(e.day,e.slot_number,e.room_id)]=True
        occ_fac[(e.day,e.slot_number,e.faculty_id)]=True

    total_lab_courses=len([s for s in subjects if s.subject_type=='lab'])
    total_lp=sum(max(1,s.hours_per_week//2) for s in subjects if s.subject_type=='lab')
    MAX_LPD=1 if total_lab_courses<5 else (2 if total_lp>4 else 1)
    print(f"ðŸ“‹ {total_lab_courses} lab courses | {total_lp} lab pairs | MAX_LPD={MAX_LPD} | {len(existing)} existing pre-loaded")

    def room_free(day,slot,is_lab):
        for r in (labs if is_lab else classrooms):
            if (day,slot,r.id) not in occ_room: return r.id
        return None

    def get_fids(s):
        if s.faculty_ids:
            ids=[int(x) for x in s.faculty_ids.split(',') if x.strip().isdigit()]
            return ids if ids else ([s.faculty_id] if s.faculty_id else [])
        return [s.faculty_id] if s.faculty_id else []

    def find_lab_quad(fids,sid,relax=False):
        if subj_days.get(sid): return None,None,None
        days=DAYS[:]; random.shuffle(days)
        blocks=QUAD_BLOCKS[:]; random.shuffle(blocks)
        for day in days:
            if not relax and day_lab_count.get(day,0)>0: continue
            for block in blocks:
                if any((day,x) in occ_grp for x in block): continue
                if any((day,x,fid) in occ_fac for x in block for fid in fids): continue
                rlabs=labs[:]; random.shuffle(rlabs)
                for r in rlabs:
                    if all((day,x,r.id) not in occ_room for x in block):
                        return day,block[0],r.id
        return None,None,None

    def find_lab_pair(fids,sid,relax_day=False,relax_lpd=False):
        used=subj_days.get(sid,set())
        days=DAYS[:]; random.shuffle(days)
        MORNING=[1,3]; AFTERNOON=[5,7]
        for day in days:
            if not relax_day and day in used: continue
            if not relax_lpd and day_lab_count.get(day,0)>=MAX_LPD: continue
            morning_used=any((day,x) in occ_grp for x in [1,2,3,4])
            afternoon_used=any((day,x) in occ_grp for x in [5,6,7,8])
            if morning_used and afternoon_used: continue
            pairs=AFTERNOON if morning_used else (MORNING if afternoon_used else (MORNING+AFTERNOON))
            random.shuffle(pairs)
            for slot in pairs:
                p=LAB_PAIRS[slot]
                if (day,slot) in occ_grp or (day,p) in occ_grp: continue
                if any((day,slot,fid) in occ_fac or (day,p,fid) in occ_fac for fid in fids): continue
                rlabs=labs[:]; random.shuffle(rlabs)
                for r in rlabs:
                    if (day,slot,r.id) not in occ_room and (day,p,r.id) not in occ_room:
                        return day,slot,r.id
        if not relax_day: return find_lab_pair(fids,sid,relax_day=True,relax_lpd=relax_lpd)
        if not relax_lpd: return find_lab_pair(fids,sid,relax_day=True,relax_lpd=True)
        return None,None,None

    def find_theory(fid,sid,relax_fac=False):
        used=subj_days.get(sid,set())
        days=DAYS[:]; random.shuffle(days)
        slots=list(range(1,9)); random.shuffle(slots)
        for day in days:
            if day in used: continue
            for slot in slots:
                if (day,slot) in occ_grp: continue
                if not relax_fac and (day,slot,fid) in occ_fac: continue
                rid=room_free(day,slot,False)
                if rid: return day,slot,rid
        if not relax_fac: return find_theory(fid,sid,relax_fac=True)
        return None,None,None

    def mark_quad(day,start_slot,fids,rid,sid):
        block=next(b for b in QUAD_BLOCKS if b[0]==start_slot)
        for x in block:
            for fid in fids: occ_fac[(day,x,fid)]=True
            occ_room[(day,x,rid)]=True; occ_grp[(day,x)]=True
        day_lab_count[day]=MAX_LPD
        subj_days.setdefault(sid,set()).add(day)

    def mark_pair(day,slot,fids,rid,sid):
        p=LAB_PAIRS[slot]
        for fid in fids:
            occ_fac.update({(day,slot,fid):True,(day,p,fid):True})
        occ_room.update({(day,slot,rid):True,(day,p,rid):True})
        occ_grp[(day,slot)]=True; occ_grp[(day,p)]=True
        day_lab_count[day]=day_lab_count.get(day,0)+1
        subj_days.setdefault(sid,set()).add(day)

    def mark_theory(day,slot,fid,rid,sid):
        occ_fac[(day,slot,fid)]=True; occ_room[(day,slot,rid)]=True
        occ_grp[(day,slot)]=True; subj_days.setdefault(sid,set()).add(day)

    placed=0
    TimetableEntry.query.filter_by(dept_id=dept_id,semester=semester,year=year).delete()
    db.session.commit()

    def save_slot(day,slot,fid,rid,sid,is_lab,fids_str=''):
        si=slot_map.get(slot,{})
        db.session.add(TimetableEntry(dept_id=dept_id,year=year,semester=semester,
            subject_id=sid,faculty_id=fid,faculty_ids=fids_str,room_id=rid,
            day=day,slot_number=slot,
            start_time=si.get('start',''),end_time=si.get('end',''),is_lab=is_lab,created_by='AI'))

    def save_pair(day,slot,fids,rid,sid):
        primary=fids[0] if fids else None
        fids_str=','.join(str(i) for i in fids)
        save_slot(day,slot,primary,rid,sid,True,fids_str)
        save_slot(day,LAB_PAIRS[slot],primary,rid,sid,True,fids_str)

    def save_quad(day,start_slot,fids,rid,sid):
        block=next(b for b in QUAD_BLOCKS if b[0]==start_slot)
        primary=fids[0] if fids else None
        fids_str=','.join(str(i) for i in fids)
        for x in block:
            save_slot(day,x,primary,rid,sid,True,fids_str)

    lab_subjects=[x for x in subjects if x.subject_type=='lab']
    theory_subjects=[x for x in subjects if x.subject_type=='theory']
    random.shuffle(lab_subjects); random.shuffle(theory_subjects)
    placed_sids=set()

    for s in lab_subjects:
        if s.id in placed_sids: continue
        placed_sids.add(s.id)
        target=s.hours_per_week; got=0
        fids=get_fids(s)

        if target>=4:
            nd,ns,nr=find_lab_quad(fids,s.id)
            if not nd: nd,ns,nr=find_lab_quad(fids,s.id,relax=True)
            if nd:
                mark_quad(nd,ns,fids,nr,s.id)
                save_quad(nd,ns,fids,nr,s.id)
                got=4; placed+=4
                print(f"âœ… Quad {s.name} â†’ {nd} P{ns}-P{ns+3}")

        for _ in range((target-got)//2):
            nd,ns,nr=find_lab_pair(fids,s.id)
            if nd:
                mark_pair(nd,ns,fids,nr,s.id)
                save_pair(nd,ns,fids,nr,s.id)
                got+=2; placed+=2
            else:
                print(f"âš ï¸ {s.name}: pair unplaced (got {got}/{target})")
        if got<target:
            print(f"âŒ {s.name}: {got}/{target} hours placed")

    for s in theory_subjects:
        if s.id in placed_sids: continue
        placed_sids.add(s.id)
        target=s.hours_per_week; got=0
        for _ in range(target):
            nd,ns,nr=find_theory(s.faculty_id,s.id)
            if nd:
                mark_theory(nd,ns,s.faculty_id,nr,s.id)
                save_slot(nd,ns,s.faculty_id,nr,s.id,False)
                got+=1; placed+=1
            else:
                print(f"âš ï¸ {s.name}: slot unplaced (got {got}/{target})")
        if got<target:
            print(f"âŒ {s.name}: {got}/{target} hours placed")

    db.session.commit()
    print(f"âœ… Placed {placed} entries")

    unmet_subjects=[s for s in subjects
        if TimetableEntry.query.filter_by(
            dept_id=dept_id,year=year,semester=semester,subject_id=s.id
        ).filter(TimetableEntry.created_by!='FILLER').count() < s.hours_per_week]

    if unmet_subjects:
        names=', '.join(s.name for s in unmet_subjects[:3])
        extra='â€¦' if len(unmet_subjects)>3 else ''
        print(f"âš ï¸ {len(unmet_subjects)} course(s) incomplete â€” skipping fillers: {names}{extra}")
        return jsonify({
            'message':f'Generated {placed} entries. {len(unmet_subjects)} course(s) could not be fully allocated â€” no filler slots added.',
            'count':placed,
            'warning':f'Incomplete: {names}{extra}'
        })

    FILLER_LIBRARY='Library Hour'
    FILLER_MENTOR='Mentor Meeting Hour'
    filler_room=classrooms[0].id if classrooms else (labs[0].id if labs else None)
    filler_count=0; mentor_placed=False
    for day in DAYS:
        for slot in range(1,9):
            if (day,slot) not in occ_grp and filler_room:
                if not mentor_placed:
                    label=FILLER_MENTOR; mentor_placed=True
                else:
                    label=FILLER_LIBRARY
                si=slot_map.get(slot,{})
                db.session.add(TimetableEntry(
                    dept_id=dept_id,year=year,semester=semester,
                    subject_id=None,faculty_id=None,room_id=filler_room,
                    day=day,slot_number=slot,
                    start_time=si.get('start',''),end_time=si.get('end',''),
                    is_lab=False,created_by='FILLER',notes=label))
                filler_count+=1
    db.session.commit()
    total=placed+filler_count
    print(f"âœ… {filler_count} filler slots added (Library/Mentor)")
    return jsonify({'message':f'Generated {placed} entries + {filler_count} filler slots','count':total})

@app.route('/api/import/excel', methods=['POST'])
def import_excel():
    if 'file' not in request.files: return jsonify({'error':'No file'}), 400
    wb=openpyxl.load_workbook(BytesIO(request.files['file'].read()))
    res={'dept_id':None,'faculty':{'added':0,'updated':0},'subjects':{'added':0,'updated':0},'rooms':{'added':0,'updated':0},'warnings':[],'errors':[]}

    def sheet_rows(name):
        if name not in wb.sheetnames: return []
        ws=wb[name]; hdrs=[str(c.value).strip().lower() if c.value else '' for c in ws[1]]
        return [dict(zip(hdrs,[str(c.value).strip() if c.value is not None else '' for c in row]))
                for row in ws.iter_rows(min_row=2) if any(c.value for c in row)]

    rows=sheet_rows('Department')
    if not rows: return jsonify({'error':'Empty Department sheet'}), 400
    r=rows[0]; code=r.get('code','').upper().strip(); dname=r.get('name','').strip()
    dept=Department.query.filter_by(code=code).first()
    if not dept:
        dept=Department(name=dname,code=code); db.session.add(dept); db.session.flush()
    else:
        dept.name=dname; db.session.flush()
    res['dept_id']=dept.id; fid_map={}

    for r in sheet_rows('Faculty'):
        name=r.get('name','').strip(); fid=r.get('faculty_id','').upper().strip()
        if not name or not fid: continue
        role=r.get('role','faculty').strip() or 'faculty'
        ex=Faculty.query.filter_by(faculty_id=fid,dept_id=dept.id).first()
        if ex:
            ex.name=name; ex.role=role
            fid_map[fid]=ex.id; res['faculty']['updated']+=1
        else:
            f=Faculty(name=name,faculty_id=fid,dept_id=dept.id,role=role)
            db.session.add(f); db.session.flush()
            fid_map[fid]=f.id; res['faculty']['added']+=1
    db.session.flush()

    for r in sheet_rows('Subjects'):
        name=r.get('name','').strip(); code=r.get('code','').strip()
        if not name or not code: continue
        try: yr,sem,hrs=int(float(r.get('year',1))),int(float(r.get('semester',1))),int(float(r.get('hours_per_week',3)))
        except: yr,sem,hrs=1,1,3
        stype=(r.get('type') or r.get('subject_type','theory')).strip().lower()
        fref_raw=(r.get('faculty_id') or '').strip().upper()
        frefs=[f.strip() for f in fref_raw.split(',') if f.strip()]
        fdbids=[fid_map[f] for f in frefs if f in fid_map]
        missing=[f for f in frefs if f not in fid_map]
        if missing: res['warnings'].append(f"Subject {code}: faculty {missing} not found")
        primary_fdbid=fdbids[0] if fdbids else None
        faculty_ids_str=','.join(str(i) for i in fdbids)
        ex=Subject.query.filter(Subject.dept_id==dept.id,
                                Subject.code.in_([code,code+' ',code.strip()])).first()
        if not ex: ex=Subject.query.filter_by(dept_id=dept.id,name=name).first()
        if ex:
            ex.name=name; ex.code=code; ex.year=yr; ex.semester=sem
            ex.subject_type=stype; ex.hours_per_week=hrs
            ex.faculty_id=primary_fdbid; ex.faculty_ids=faculty_ids_str
            res['subjects']['updated']+=1
        else:
            db.session.add(Subject(name=name,code=code,year=yr,semester=sem,dept_id=dept.id,
                                   subject_type=stype,hours_per_week=hrs,
                                   faculty_id=primary_fdbid,faculty_ids=faculty_ids_str))
            res['subjects']['added']+=1
        db.session.flush()

    for r in sheet_rows('Rooms'):
        name=r.get('name','').strip()
        if not name: continue
        try: cap=int(float(r.get('capacity',60) or 60))
        except: cap=60
        rtype=(r.get('type') or r.get('room_type','classroom')).strip().lower()
        ex=Room.query.filter_by(name=name,dept_id=dept.id).first()
        if ex:
            ex.room_type=rtype; ex.capacity=cap
            res['rooms']['updated']+=1
        else:
            db.session.add(Room(name=name,room_type=rtype,capacity=cap,dept_id=dept.id))
            res['rooms']['added']+=1
        db.session.flush()

    db.session.commit()
    fa=res['faculty']; su=res['subjects']; ro=res['rooms']
    return jsonify({'success':True,'dept_id':res['dept_id'],
                    'imported':{
                        'faculty':f"{fa['added']} added, {fa['updated']} updated",
                        'subjects':f"{su['added']} added, {su['updated']} updated",
                        'rooms':f"{ro['added']} added, {ro['updated']} updated"
                    },
                    'warnings':res['warnings'],'errors':res['errors']})

@app.route('/api/import/bulk', methods=['POST'])
def bulk_import():
    d=request.json; dd=d.get('department',{})
    if not dd.get('name') or not dd.get('code'): return jsonify({'error':'Dept name+code required'}), 400
    code=dd['code'].upper(); dept=Department.query.filter_by(code=code).first()
    if not dept:
        dept=Department(name=dd['name'],code=code); db.session.add(dept); db.session.flush()
    res={'dept_id':dept.id,'faculty':[],'subjects':[],'rooms':[]}; fid_map={}

    for f in d.get('faculty',[]):
        if not f.get('name') or not f.get('faculty_id'): continue
        fid=f['faculty_id'].upper()
        ex=Faculty.query.filter_by(faculty_id=fid,dept_id=dept.id).first()
        if ex: fid_map[fid]=ex.id; continue
        obj=Faculty(name=f['name'],faculty_id=fid,dept_id=dept.id,
                    role=f.get('role','faculty'))
        db.session.add(obj); db.session.flush(); fid_map[fid]=obj.id; res['faculty'].append({'id':obj.id})

    for s in d.get('subjects',[]):
        if not s.get('name') or not s.get('code'): continue
        if Subject.query.filter_by(code=s['code'],dept_id=dept.id).first(): continue
        fdbid=fid_map.get((s.get('faculty_id') or '').upper())
        obj=Subject(name=s['name'],code=s['code'],year=int(s.get('year',1)),semester=int(s.get('semester',1)),
                    dept_id=dept.id,subject_type=s.get('subject_type','theory'),
                    hours_per_week=int(s.get('hours_per_week',3)),faculty_id=fdbid)
        db.session.add(obj); db.session.flush(); res['subjects'].append({'id':obj.id})

    for r in d.get('rooms',[]):
        if not r.get('name'): continue
        if Room.query.filter_by(name=r['name'],dept_id=dept.id).first(): continue
        obj=Room(name=r['name'],room_type=r.get('room_type','classroom'),
                 capacity=int(r.get('capacity',60)),dept_id=dept.id)
        db.session.add(obj); db.session.flush(); res['rooms'].append({'id':obj.id})

    db.session.commit()
    return jsonify({'success':True,'dept_id':res['dept_id'],
                    'imported':{'faculty':len(res['faculty']),'subjects':len(res['subjects']),'rooms':len(res['rooms'])}})

@app.route('/api/slots')
def get_slots(): return jsonify({'days':DAYS,'slots':SLOTS})

@app.route('/api/auth/register',methods=['POST'])
def register_user():
    d=request.json or {};username=d.get('username','').strip();email=d.get('email','').strip().lower();password=d.get('password','')
    if not username or not email or len(password)<6:return jsonify({'error':'Username, email and a 6+ character password are required'}),400
    if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():return jsonify({'error':'Username or email already exists'}),409
    u=User(username=username,email=email,country=d.get('country','').strip(),password_hash=generate_password_hash(password));db.session.add(u);db.session.commit();session['user_id']=u.id
    return jsonify({'id':u.id,'username':u.username,'email':u.email,'country':u.country}),201

@app.route('/api/auth/login',methods=['POST'])
def login_user():
    d=request.json or {};u=User.query.filter_by(username=d.get('username','').strip()).first()
    if not u or not check_password_hash(u.password_hash,d.get('password','')):return jsonify({'error':'Invalid username or password'}),401
    session['user_id']=u.id;return jsonify({'id':u.id,'username':u.username,'email':u.email,'country':u.country})

@app.route('/api/auth/logout',methods=['POST'])
def logout_user():session.clear();return jsonify({'success':True})

@app.route('/api/auth/me')
def current_user():
    u=User.query.get(session.get('user_id')) if session.get('user_id') else None
    return jsonify({'authenticated':bool(u),'user':({'id':u.id,'username':u.username,'email':u.email,'country':u.country} if u else None)})

@app.route('/api/student-groups',methods=['GET','POST'])
def student_groups():
    if request.method=='GET':
        q=StudentGroup.query
        for k in ('dept_id','year','semester'):
            if request.args.get(k):q=q.filter_by(**{k:int(request.args[k])})
        return jsonify([g._doc() for g in q.all()])
    d=request.json or {}
    if not d.get('name'):return jsonify({'error':'Batch name is required'}),400
    g=StudentGroup(name=d['name'].strip(),dept_id=_coerce(d.get('dept_id')),year=_coerce(d.get('year')),semester=_coerce(d.get('semester')),subjects=d.get('subjects',[]),created_by=str(session.get('user_id','')))
    db.session.add(g);db.session.commit();return jsonify(g._doc()),201

@app.route('/api/student-groups/<int:gid>',methods=['PUT','DELETE'])
def student_group_detail(gid):
    g=StudentGroup.query.get_or_404(gid)
    if request.method=='DELETE':db.session.delete(g);db.session.commit();return jsonify({'deleted':gid})
    for k,v in (request.json or {}).items():
        if k in g.fields and k!='id':setattr(g,k,v)
    db.session.commit();return jsonify(g._doc())

@app.route('/api/generate/genetic',methods=['POST'])
def generate_genetic():
    d=request.json or {};dept_id=_coerce(d.get('dept_id'));year=_coerce(d.get('year'));semester=_coerce(d.get('semester'))
    subjects=Subject.query.filter_by(dept_id=dept_id,year=year,semester=semester).all();rooms=Room.query.filter_by(dept_id=dept_id).all()
    if not subjects or not rooms:return jsonify({'error':'Subjects and rooms are required'}),400
    population=max(20,min(int(d.get('population_size',200)),1000));generations=max(1,min(int(d.get('max_generations',100)),500));mutation=float(d.get('mutation_rate',.1))
    required=[s for s in subjects for _ in range(max(1,int(s.hours_per_week or 1)))];cells=[(day,slot) for day in DAYS for slot in range(1,9)]
    if len(required)>len(cells):return jsonify({'error':f'{len(required)} required periods exceed {len(cells)} available group slots'}),400
    def chromosome():return [{'subject':s,'day':c[0],'slot':c[1]} for s,c in zip(required,random.sample(cells,len(required)))]
    def score(ch):
        penalty=0;fac=set();same_day=set()
        for x in ch:
            s=x['subject'];fk=(s.faculty_id,x['day'],x['slot']);dk=(s.id,x['day'])
            if s.faculty_id and fk in fac:penalty+=20
            if dk in same_day:penalty+=2
            fac.add(fk);same_day.add(dk)
        return 1/(1+penalty)
    pop=[chromosome() for _ in range(population)];best=None;best_score=0;generation=0
    for generation in range(generations):
        ranked=sorted(((score(c),c) for c in pop),key=lambda x:x[0],reverse=True)
        if ranked[0][0]>best_score:best_score,best=ranked[0]
        if best_score>=1:break
        elite=[c for _,c in ranked[:max(2,population//10)]];new=elite[:]
        while len(new)<population:
            a=[dict(x) for x in random.choice(elite)];b=random.choice(elite);cut=random.randrange(len(a));child=a[:cut]+[dict(x) for x in b[cut:]];used=set()
            for x in child:
                key=(x['day'],x['slot'])
                if key in used:x['day'],x['slot']=random.choice([c for c in cells if c not in used])
                used.add((x['day'],x['slot']))
            if random.random()<mutation and len(child)>1:
                i,j=random.sample(range(len(child)),2);child[i]['day'],child[j]['day']=child[j]['day'],child[i]['day'];child[i]['slot'],child[j]['slot']=child[j]['slot'],child[i]['slot']
            new.append(child)
        pop=new
    TimetableEntry.query.filter_by(dept_id=dept_id,year=year,semester=semester).delete();room_index=0
    for x in best:
        s=x['subject'];suitable=[r for r in rooms if r.room_type==('lab' if s.subject_type=='lab' else 'classroom')] or rooms;r=suitable[room_index%len(suitable)];room_index+=1;slot=SLOTS[x['slot']-1]
        db.session.add(TimetableEntry(dept_id=dept_id,year=year,semester=semester,subject_id=s.id,faculty_id=s.faculty_id,faculty_ids=s.faculty_ids,room_id=r.id,day=x['day'],slot_number=x['slot'],start_time=slot['start'],end_time=slot['end'],is_lab=s.subject_type=='lab',created_by='GENETIC'))
    db.session.commit();return jsonify({'message':'Genetic timetable generated','count':len(best),'fitness':best_score,'generations':generation+1,'population_size':population})

def _legacy_data_from_json(d):
    groups=[]
    for i,g in enumerate(d.get('student_groups',d.get('studentgroup',[]))):
        if isinstance(g,str):continue
        subjects=g.get('subjects',[]);names=[s.get('name',s.get('subject','')) if isinstance(s,dict) else str(s) for s in subjects];hours=[int(s.get('hours',s.get('subjecttime',1))) if isinstance(s,dict) else 1 for s in subjects]
        groups.append(LegacyStudentGroup(i,g.get('name',g.get('studentgroup',f'Group {i+1}')),names,hours))
    teachers=[LegacyTeacher(i,t.get('name',t.get('teacher',f'Teacher {i+1}')),t.get('subject',t.get('teachersubject',''))) for i,t in enumerate(d.get('teachers',d.get('teacher',[]))) if isinstance(t,dict)]
    hours=int(d.get('hours_per_day',d.get('hoursperday',7)));days=int(d.get('days_per_week',d.get('daysperweek',5)))
    day_names=d.get('day_names') or d.get('days') or []
    period_times=d.get('period_times') or [{'slot':i+1,'start':(d.get('start') or ['']*hours)[i] if i<len(d.get('start') or []) else '','end':(d.get('end') or ['']*hours)[i] if i<len(d.get('end') or []) else ''} for i in range(hours)]
    return InputData(groups,teachers,hours_per_day=hours,days_per_week=days,day_names=day_names,period_times=period_times,break_slot=_coerce(d.get('break_slot',d.get('breakslot'))),break_start=d.get('break_start',d.get('breakstart')),break_end=d.get('break_end',d.get('breakend')),crossover_rate=float(d.get('crossover_rate',1)),mutation_rate=float(d.get('mutation_rate',.1)))

def _run_legacy(data,options,source):
    engine=LegacyScheduler(data,population_size=int(options.get('population_size',1000)),max_generations=int(options.get('max_generations',100)),seed=options.get('seed'),mutation_attempts=int(options.get('mutation_attempts',5000)))
    chromosome=engine.run();rendered=engine.render(chromosome);summary=input_summary(data)
    run=LegacyScheduleRun(owner_id=session.get('user_id'),source=source,configuration={'hours_per_day':data.hours_per_day,'days_per_week':data.days_per_week,'day_names':data.day_names,'period_times':data.period_times,'break_slot':data.break_slot,'break_start':data.break_start,'break_end':data.break_end,'population_size':engine.population_size,'max_generations':engine.max_generations,'crossover_rate':data.crossover_rate,'mutation_rate':data.mutation_rate},input=summary,fitness=chromosome.fitness,collisions=chromosome.collisions,history=engine.history,timetables=rendered)
    db.session.add(run);db.session.commit()
    return run,{'run_id':run.id,'fitness':run.fitness,'collisions':run.collisions,'chromosome':chromosome.raw(),'history':run.history,'input':summary,'slots':slot_summary(engine.timetable),'timetables':rendered}

@app.route('/api/legacy/generate',methods=['POST'])
def legacy_generate_form():
    try:
        d=request.json or {};run,result=_run_legacy(_legacy_data_from_json(d),d,'form');return jsonify(result),201
    except (ValueError,TypeError) as e:return jsonify({'error':str(e)}),400

@app.route('/api/legacy/generate/file',methods=['POST'])
def legacy_generate_file():
    try:
        if request.files.get('file'):text=request.files['file'].read().decode('utf-8-sig')
        elif request.is_json:text=(request.json or {}).get('text','')
        else:text=request.get_data(as_text=True)
        opts=(request.form.to_dict() if request.files else (request.json or {}))
        settings={'hours_per_day':int(opts.get('hours_per_day',7)),'days_per_week':int(opts.get('days_per_week',5)),'crossover_rate':float(opts.get('crossover_rate',1)),'mutation_rate':float(opts.get('mutation_rate',.1))}
        data=parse_legacy_text(text,**settings);run,result=_run_legacy(data,opts,'file');return jsonify(result),201
    except (UnicodeDecodeError,ValueError,TypeError) as e:return jsonify({'error':str(e)}),400

@app.route('/api/legacy/input/class-format',methods=['POST'])
def legacy_class_format():
    line=(request.json or {}).get('line','') if request.is_json else request.get_data(as_text=True)
    return jsonify({'valid':class_format(line),'token_count':len(line.split())})

@app.route('/api/legacy/runs',methods=['GET'])
def legacy_runs():
    return jsonify([{'id':r.id,'owner_id':r.owner_id,'source':r.source,'fitness':r.fitness,'collisions':r.collisions,'configuration':r.configuration,'created_at':r.created_at.isoformat()} for r in LegacyScheduleRun.query.all()])

@app.route('/api/legacy/runs/<int:run_id>',methods=['GET','DELETE'])
def legacy_run_detail(run_id):
    run=LegacyScheduleRun.query.get_or_404(run_id)
    if request.method=='DELETE':db.session.delete(run);db.session.commit();return jsonify({'deleted':run_id})
    return jsonify(run._doc())

@app.route('/api/legacy/runs/<int:run_id>/export',methods=['GET'])
def legacy_export(run_id):
    run=LegacyScheduleRun.query.get_or_404(run_id);fmt=request.args.get('format','txt').lower()
    if fmt=='json':
        payload=json.dumps(run._doc(),default=str,indent=2).encode();return send_file(BytesIO(payload),mimetype='application/json',as_attachment=True,download_name=f'timetable-{run_id}.json')
    lines=[f"Timetable Run {run.id}",f"Fitness: {run.fitness:.6f} | Teacher collisions: {run.collisions}",'']
    for group in run.timetables:
        lines.append(f"Batch {group['group_name']} Timetable")
        for day in group['days']:lines.append(day['day']+': '+' | '.join((p['subject'] or '*FREE*')+(f" ({p['teacher_name']})" if p['teacher_name'] else '') for p in day['periods']))
        lines.append('')
    payload='\n'.join(lines).encode('utf-8');return send_file(BytesIO(payload),mimetype='text/plain',as_attachment=True,download_name=f'timetable-{run_id}.txt')

@app.route('/api/support/contact',methods=['POST'])
def support_contact():
    d=request.json or {};required=('name','email','message')
    if any(not str(d.get(k,'')).strip() for k in required):return jsonify({'error':'Name, email and message are required'}),400
    doc={'name':d['name'].strip(),'email':d['email'].strip(),'phone':str(d.get('phone','')).strip(),'message':d['message'].strip(),'user_id':session.get('user_id'),'created_at':datetime.utcnow(),'status':'new'}
    result=mongo_db.support_messages.insert_one(doc);return jsonify({'id':str(result.inserted_id),'status':'received'}),201

@app.route('/api/health')
def health():
    try:return jsonify({'status':'ok','database':'mongodb','entries':TimetableEntry.query.count()})
    except Exception as exc:return jsonify({'status':'degraded','database':'mongodb-unavailable','error':'Set a reachable MONGODB_URI environment variable'}),503

@app.route('/')
def serve_index(): return send_from_directory(str(FRONTEND_DIR),'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if path.startswith('api/'): return jsonify({'error':'Not found'}), 404
    return send_from_directory(str(FRONTEND_DIR), path)

with app.app_context():
    try:
        mongo.admin.command('ping')
        mongo_db.departments.create_index([('code',ASCENDING)],unique=True)
        mongo_db.faculty.create_index([('faculty_id',ASCENDING),('dept_id',ASCENDING)],unique=True,sparse=True)
        mongo_db.users.create_index([('username',ASCENDING)],unique=True)
        mongo_db.users.create_index([('email',ASCENDING)],unique=True)
        print('-'*40+' EduSchedule MongoDB Ready -> http://127.0.0.1:5000')
    except Exception:
        print('MongoDB unavailable at startup; configure MONGODB_URI for data APIs')

if __name__ == '__main__':
    app.run(debug=True, port=5000)

