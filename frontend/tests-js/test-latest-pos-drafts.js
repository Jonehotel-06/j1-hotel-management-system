const test=require('node:test');
const assert=require('node:assert/strict');
const DraftBook=require('../js/pos-drafts.js');
function fixture(){const data=new Map();const storage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};let n=0;return {storage,data,key:()=>`draft-${++n}`};}
test('restaurant and bar carts keep independent modes, quantities, category, guest/table and retry keys',()=>{
 const {storage,key}=fixture(),book=new DraftBook(storage,12,key);
 let restaurant=book.create('RESTAURANT');restaurant.cart=[{menu_item_id:1,quantity:2}];restaurant.category='food';restaurant.fields={'pos-guest':'Alice','pos-table':'1'};restaurant.selectedTableSession='TABLE-A';book.save(restaurant);
 let bar=book.create('BAR');bar.cart=[{menu_item_id:2,quantity:3}];bar.category='drinks';bar.fields={'pos-guest':'Bob','pos-table':'6'};book.save(bar);
 assert.notEqual(restaurant.orderKey,bar.orderKey);assert.equal(book.rows.length,2);
 const restored=book.select(restaurant.id);assert.deepEqual(restored.cart,[{menu_item_id:1,quantity:2}]);assert.equal(restored.selectedTableSession,'TABLE-A');assert.equal(restored.category,'food');assert.equal(restored.fields['pos-guest'],'Alice');assert.equal(book.get(bar.id).fields['pos-guest'],'Bob');
});
test('editing a detached snapshot cannot accidentally mutate another cart',()=>{const {storage,key}=fixture(),book=new DraftBook(storage,12,key),a=book.create('BAR');a.cart.push({quantity:9});assert.deepEqual(book.get(a.id).cart,[]);book.save(a);const b=book.create('BAR');assert.deepEqual(b.cart,[]);assert.equal(book.get(a.id).cart[0].quantity,9);});
test('browser session reload retains all unfinished carts and active selection',()=>{const {storage,key}=fixture(),first=new DraftBook(storage,12,key),a=first.create('BAR');a.cart=[{quantity:2}];first.save(a);first.create('RESTAURANT');first.select(a.id);const reloaded=new DraftBook(storage,12,key);assert.equal(reloaded.rows.length,2);assert.equal(reloaded.active,a.id);assert.equal(reloaded.get(a.id).cart[0].quantity,2);});
test('staff identities are isolated and cannot save another employee cart',()=>{const {storage,key}=fixture(),a=new DraftBook(storage,12,key),cart=a.create('BAR'),b=new DraftBook(storage,20,key);assert.equal(b.rows.length,0);assert.throws(()=>b.save(cart),/another staff/);assert.throws(()=>new DraftBook(storage,null,key),/identity/);});
test('successful independent submission removes only its own browser cart',()=>{const {storage,key}=fixture(),book=new DraftBook(storage,12,key),a=book.create('BAR'),b=book.create('RESTAURANT');book.remove(a.id);assert.equal(book.rows.length,1);assert.equal(book.get(b.id).mode,'RESTAURANT');assert.throws(()=>book.get(a.id),/not found/);});
test('saved projection does not serialize credentials or arbitrary auth state',()=>{const {storage,key,data}=fixture(),book=new DraftBook(storage,12,key),a=book.create('BAR');a.access='must-not-be-saved';a.fields={'pos-guest':'Alice',password:'must-not-be-saved'};book.save(a);const raw=data.get(book.key);assert.ok(!raw.includes('must-not-be-saved'));assert.ok(!raw.includes('password'));});
test('storage failure is visible rather than silently reporting persistence',()=>{const {key}=fixture(),storage={getItem:()=>null,setItem:()=>{throw new Error('quota exceeded');}},book=new DraftBook(storage,12,key);assert.throws(()=>book.create('BAR'),/quota exceeded/);});
test('corrupt browser storage falls back to an empty staff-scoped book',()=>{const {key}=fixture(),book=new DraftBook({getItem:()=>'{broken',setItem:()=>{}},12,key);assert.deepEqual(book.rows,[]);});
