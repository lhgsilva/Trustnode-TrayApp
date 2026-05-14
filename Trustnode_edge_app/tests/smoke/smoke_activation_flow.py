#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
import requests
BASE_URL=os.environ.get('TN_BASE_URL','http://127.0.0.1:8000').rstrip('/')
TENANT_ID=os.environ.get('TN_TENANT_ID','default')
ADMIN_USER=os.environ.get('TN_ADMIN_USER','admin')
ADMIN_PASS=os.environ.get('TN_ADMIN_PASS','admin')
OUT_DIR=os.environ.get('TN_SMOKE_REPORT_DIR',os.path.join('tests','reports'))

def now_iso(): return datetime.now(timezone.utc).isoformat()

def req(s,m,p,**k):
  u=f"{BASE_URL}{p}"
  for a in range(1,6):
    try: return s.request(m,u,timeout=20,**k)
    except requests.RequestException:
      if a==5: raise
      time.sleep(0.6*a)

def ok(r,l):
  if r.status_code>=400:
    try:d=json.dumps(r.json())
    except Exception:d=r.text
    raise RuntimeError(f"{l} failed ({r.status_code}): {d}")
  return r.json() if r.text else {}

def main():
  run_id=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
  cid=f"smk-cust-{run_id}"; eid=f"smk-edge-{run_id}"; lid=f"smk-lic-{run_id}"; en=f"SMK Edge {run_id}"
  rep={"run_id":run_id,"started_utc":now_iso(),"base_url":BASE_URL,"tenant_id":TENANT_ID,"customer_id":cid,"edge_id":eid,"license_id":lid,"steps":[],"ok":False}
  s=requests.Session()
  d=ok(req(s,'POST','/api/auth/login',json={'username':ADMIN_USER,'password':ADMIN_PASS}),'login')
  t=str(d.get('token') or '')
  if not t: raise RuntimeError('no token')
  s.headers.update({'Authorization':f'Bearer {t}'})
  rep['steps'].append({'step':'login','ok':True})
  cat=ok(req(s,'GET','/api/control-plane/modules'),'modules')
  mods=[{'module_key':str(r.get('key') or '').strip(),'enabled':True} for r in (cat.get('modules') or []) if str(r.get('key') or '').strip()]
  if not mods: raise RuntimeError('empty module catalog')
  ok(req(s,'POST',f'/api/control-plane/customers?tenant_id={TENANT_ID}',json={'customer_id':cid,'company_name':f'Smoke Customer {run_id}','contact_email':f'smoke+{run_id}@example.com','status':'active','metadata':{'source':'smoke_test'}}),'customer')
  ok(req(s,'POST',f'/api/control-plane/edges?tenant_id={TENANT_ID}',json={'edge_id':eid,'edge_name':en,'customer_id':cid,'site':'Smoke Site','area':'Line A','equipment':'SMK','status':'active','metadata':{'source':'smoke_test'}}),'edge')
  start_dt=datetime.now(timezone.utc); end_dt=start_dt+timedelta(days=365)
  ok(req(s,'POST',f'/api/control-plane/licenses?tenant_id={TENANT_ID}',json={'license_id':lid,'customer_id':cid,'plan_code':'standard','status':'active','start_utc':start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],'end_utc':end_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],'max_edges':5,'max_users':25,'metadata':{'source':'smoke_test'}}),'license')
  ok(req(s,'PUT',f'/api/control-plane/licenses/{lid}/modules',json={'modules':mods}),'set modules')
  issue=ok(req(s,'POST',f'/api/control-plane/activation-code/issue?tenant_id={TENANT_ID}',json={'customer_id':cid,'edge_id':eid,'license_id':lid,'edge_name':en,'ttl_minutes':120,'metadata':{'source':'smoke_test'}}),'issue code')
  ac=str(((issue.get('row') or {}).get('activation_code') or '')).strip()
  if not ac: raise RuntimeError('missing activation code')
  reg=ok(req(s,'POST','/api/control-plane/edge-link/register',json={'activation_code':ac,'edge_id':eid,'edge_name':en,'site':'Smoke Site','area':'Line A','equipment':'SMK','admin_username':ADMIN_USER,'admin_password':ADMIN_PASS}),'register')
  chk=ok(req(s,'GET',f'/api/control-plane/edge-link/license-check?tenant_id={TENANT_ID}&edge_id={eid}'),'license check')
  lo=chk.get('license') or {}; mo=lo.get('modules') if isinstance(lo.get('modules'),list) else []
  asserts={'license_check_ok':bool(chk.get('ok')),'license_id_match':str(lo.get('license_id') or '')==lid,'start_present':bool(str(lo.get('start_utc') or '').strip()),'end_present':bool(str(lo.get('end_utc') or '').strip()),'modules_present':len(mo)>0,'edge_customer_present':bool(str((chk.get('edge') or {}).get('customer_id') or '').strip())}
  rep['assertions']=asserts; rep['ok']=all(asserts.values()); rep['finished_utc']=now_iso(); rep['register']=reg; rep['license_check']=chk; rep['activation_code']=ac
  os.makedirs(OUT_DIR,exist_ok=True)
  outf=os.path.join(OUT_DIR,f'smoke_activation_{run_id}.json')
  with open(outf,'w',encoding='utf-8') as f: json.dump(rep,f,indent=2)
  print(json.dumps({'ok':rep['ok'],'report':outf,'assertions':asserts,'ids':{'customer_id':cid,'edge_id':eid,'license_id':lid,'activation_code':ac}},indent=2))
  if not rep['ok']: sys.exit(2)

if __name__=='__main__': main()
