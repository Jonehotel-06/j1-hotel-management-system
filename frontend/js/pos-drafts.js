/* Independent unfinished POS carts. Credentials are never persisted here. */
(function(root){
  "use strict";
  function copy(value){return JSON.parse(JSON.stringify(value));}
  function DraftBook(storage,staffId,keyFactory){
    if(!staffId)throw new Error("A signed-in staff identity is required for carts.");
    this.storage=storage;this.staffId=String(staffId);this.keyFactory=keyFactory;
    this.key="jone.pos.drafts.v1."+this.staffId;this.rows=[];this.active=null;
    try{var saved=JSON.parse(storage.getItem(this.key)||"null");if(saved&&saved.staffId===this.staffId&&Array.isArray(saved.rows)){this.rows=saved.rows;this.active=saved.active;}}catch(_){}
  }
  DraftBook.prototype.persist=function(){
    // Never silently claim persistence succeeded when browser storage is full.
    this.storage.setItem(this.key,JSON.stringify({staffId:this.staffId,active:this.active,rows:this.rows}));
  };
  DraftBook.prototype.create=function(mode){
    var id=this.keyFactory();this.rows.push({id:id,mode:mode,staffId:this.staffId,cart:[],fields:{},orderKey:this.keyFactory(),orderKeyContent:"",selectedStay:null,selectedTableSession:null,category:""});
    this.active=id;this.persist();return this.get(id);
  };
  DraftBook.prototype.get=function(id){var row=this.rows.find(function(row){return row.id===id;});if(!row)throw new Error("Unfinished order not found.");return copy(row);};
  DraftBook.prototype.save=function(snapshot){
    if(String(snapshot.staffId)!==this.staffId)throw new Error("Cannot mix carts from another staff account.");
    var index=this.rows.findIndex(function(row){return row.id===snapshot.id;});if(index<0)throw new Error("Unfinished order not found.");
    // Explicit projection: do not serialize Auth state, tokens or unrelated page state.
    var fields={};["pos-guest","pos-table","pos-delivery","pos-stay-search"].forEach(function(key){fields[key]=String((snapshot.fields||{})[key]||"");});
    this.rows[index]={id:snapshot.id,staffId:this.staffId,mode:snapshot.mode,cart:copy(snapshot.cart||[]),fields:fields,orderKey:snapshot.orderKey,orderKeyContent:snapshot.orderKeyContent||"",selectedStay:copy(snapshot.selectedStay||null),selectedTableSession:snapshot.selectedTableSession||null,category:snapshot.category||"",serviceRequestReference:snapshot.serviceRequestReference||"",serviceRequest:copy(snapshot.serviceRequest||null)};
    this.active=snapshot.id;this.persist();
  };
  DraftBook.prototype.select=function(id){var row=this.get(id);this.active=id;this.persist();return row;};
  DraftBook.prototype.remove=function(id){this.rows=this.rows.filter(function(row){return row.id!==id;});if(this.active===id)this.active=null;this.persist();};
  if(typeof module!=="undefined"&&module.exports)module.exports=DraftBook;
  root.JONE=root.JONE||{};root.JONE.PosDraftBook=DraftBook;
})(typeof window!=="undefined"?window:globalThis);
