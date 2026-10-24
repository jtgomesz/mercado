"""End-to-end smoke test for the local Mercado Pro API. No third-party packages."""
import http.client, json, os, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'mercado'

def run():
    with tempfile.TemporaryDirectory(prefix='mercado-test-') as data_dir:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        env=os.environ.copy();env.update({'MERCADO_DATA_DIR':data_dir,'MERCADO_PORT':str(port),'PYTHONUNBUFFERED':'1'})
        proc=subprocess.Popen([sys.executable,str(ROOT/'app.py')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
                try: conn.request('GET','/api/bootstrap');r=conn.getresponse();raw=r.read();conn.close();break
                except OSError:conn.close();time.sleep(.1)
            if 'raw' not in locals():
                print('SERVER EXIT',proc.poll(),proc.stdout.read().decode(),proc.stderr.read().decode()); raise RuntimeError('server did not start')
            data=json.loads(raw);assert data['data']['setup_required'] is True
            def request(method,path,body=None,csrf=None,cookie=None):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
                headers={}
                if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
                if csrf:headers['X-CSRF-Token']=csrf
                if cookie:headers['Cookie']=cookie
                conn.request(method,path,body=body,headers=headers);res=conn.getresponse();b=res.read();out=(res.status,dict(res.getheaders()),json.loads(b) if 'application/json' in str(res.getheader('Content-Type')) else b);conn.close();return out
            status,h,d=request('POST','/api/setup',{'username':'admin','display_name':'Teste Admin','password':'SenhaMuitoForte123'})
            assert status==201,(status,d)
            cookie=h.get('Set-Cookie','').split(';',1)[0];csrf=d['data']['user']['csrf_token']
            status,h,d=request('POST','/api/categories',{'name':'Mercearia','color':'#32d583'},csrf,cookie);assert status==201,(status,d);cat=d['data']['id']
            status,h,d=request('POST','/api/products',{'sku':'TEST-001','barcode':'7899999999999','name':'Arroz Teste','category_id':cat,'unit':'un','cost':5.25,'price':9.99,'stock':10,'min_stock':2},csrf,cookie);assert status==201,(status,d);pid=d['data']['id']
            status,h,d=request('POST','/api/discounts',{'name':'Oferta teste','target_type':'product','target_id':pid,'percent':10},csrf,cookie);assert status==201,(status,d)
            status,h,d=request('GET','/api/products',cookie=cookie);p=d['data'][0];assert p['effective_discount_bp']==1000 and abs(p['sale_price']-8.99)<.001,(p)
            status,h,d=request('POST','/api/sales/checkout',{'items':[{'product_id':pid,'quantity':2}],'payment_method':'pix'},csrf,cookie);assert status==201,(status,d);sale_id=d['data']['id'];assert abs(d['data']['total']-17.98)<.001,d
            status,h,d=request('GET','/api/products',cookie=cookie);p=d['data'][0];assert p['stock']==8,p
            status,h,d=request('POST',f'/api/sales/{sale_id}/cancel',{'reason':'Teste de estorno'},csrf,cookie);assert status==200,(status,d)
            status,h,d=request('GET','/api/products',cookie=cookie);p=d['data'][0];assert p['stock']==10,p
            status,h,d=request('POST','/api/suppliers',{'name':'Fornecedor Teste','phone':'11999999999','contact':'Equipe comercial'},csrf,cookie);assert status==201,(status,d);supplier=d['data']['id']
            status,h,d=request('POST','/api/purchases',{'supplier_id':supplier,'notes':'Compra de teste','items':[{'product_id':pid,'quantity':5,'unit_cost':5.10}]},csrf,cookie);assert status==201,(status,d);purchase_id=d['data']['id']
            status,h,d=request('POST',f'/api/purchases/{purchase_id}/receive',{},csrf,cookie);assert status==200,(status,d)
            status,h,d=request('GET','/api/products',cookie=cookie);p=d['data'][0];assert p['stock']==15 and abs(p['cost']-5.10)<.001,p
            status,h,d=request('POST','/api/expenses',{'category':'Energia','description':'Conta de teste','amount':120.50,'expense_date':__import__('datetime').date.today().isoformat(),'payment_method':'pix'},csrf,cookie);assert status==201,(status,d)
            status,h,d=request('GET','/api/analytics',cookie=cookie);assert status==200 and d['data']['orders']==0,(status,d)
            status,h,d=request('GET','/api/dashboard',cookie=cookie);assert status==200 and d['data']['products_count']==1 and d['data']['expenses']>=120.50,(status,d)
            status,h,d=request('POST','/api/users',{'username':'operador1','display_name':'Operador Teste','role':'cashier','password':'OutraSenhaForte987'},csrf,cookie);assert status==201,(status,d)
            status,h,d=request('POST','/api/stock',{'product_id':pid,'kind':'adjustment','quantity':0,'note':'Contagem de teste'},csrf,cookie);assert status==200,(status,d)
            status,h,blob=request('GET','/api/backup',cookie=cookie);assert status==200 and len(blob)>1000,(status,len(blob))
            backup_path=Path(data_dir)/'downloaded-backup.sqlite3';backup_path.write_bytes(blob);db=sqlite3.connect(backup_path);assert db.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1;db.close()
            status,h,blob=request('GET','/api/export?type=products',cookie=cookie);assert status==200 and b'TEST-001' in blob
            # Session and database survive a full process restart.
            proc.terminate();proc.wait(timeout=5)
            proc=subprocess.Popen([sys.executable,str(ROOT/'app.py')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            for attempt in range(50):
                try:
                    status,h,d=request('GET','/api/products',cookie=cookie)
                    if status==200:break
                except OSError:time.sleep(.1)
            assert status==200 and d['data'][0]['stock']==0,(status,d)
            status,h,d=request('POST','/api/login',{'username':'operador1','password':'OutraSenhaForte987'});assert status==200,(status,d);cashier_cookie=h.get('Set-Cookie','').split(';',1)[0];cashier_csrf=d['data']['user']['csrf_token']
            status,h,d=request('POST','/api/stock',{'product_id':pid,'kind':'entry','quantity':1,'note':'Tentativa de operador'},cashier_csrf,cashier_cookie);assert status==403,(status,d)
            # Invalid checkout must not modify persisted stock.
            status,h,d=request('POST','/api/sales/checkout',{'items':[{'product_id':pid,'quantity':1}],'payment_method':'pix'},cashier_csrf,cashier_cookie);assert status==400,(status,d)
            print('PASS: setup/session, unique product/category storage, promotion math, checkout and stock decrement, cancellation reversal, purchase receiving/cost update, expense/analytics, role permissions, SQLite backup integrity, CSV export and persistence after process restart.')
        finally:
            proc.terminate();proc.wait(timeout=5)
if __name__=='__main__':run()
