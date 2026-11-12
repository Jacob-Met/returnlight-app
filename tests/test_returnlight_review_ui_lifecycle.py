"""Exercise the actual UI action/review functions across a fast cancellation."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

UI = Path(__file__).parents[1] / "returnlight" / "returnlight_web_ui.html"

def test_completed_review_conflict_survives_concurrent_case_action():
    if not shutil.which("node"):
        pytest.skip("Node is required for the UI lifecycle regression")
    text = UI.read_text()
    render = "function renderReviewState()" + text.split("function renderReviewState()", 1)[1].split("async function reviewCase", 1)[0]
    action = "async function runAction(" + text.split("async function runAction(", 1)[1].split("const buyerMessageRecordings", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
let busy=false,selectedKey='case',selected={id:'case',history:[],post_payment_messages:[{text:'Corrected buyer words'}]},
 selectedExample=false,decisionConflictKey=null,reviewConfiguration=null;
const reviewStates=new Map(),elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{textContent:'',hidden:false,disabled:false,dataset:{},attrs:{},
 classList:{toggle(){},remove(){},add(){}},setAttribute(k,v){this.attrs[k]=v;}});return elements.get(id);};
const document={querySelectorAll(){return[];},querySelector(){return null;}};
const renderPaymentDecision=()=>{},alertStatus=()=>{},renderFollowupDraft=()=>{},
 renderConditionReuseState=()=>{},renderBuyerUpdate=()=>{},refreshCases=async()=>{},selectCase=async()=>{};
""" + render + action + r"""
(async()=>{
 const state={pending:true,support:true,message:'Reading support…'};
 reviewStates.set(selectedKey,state);
 renderReviewState();
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'true');
 let release;
 const action=runAction(()=>new Promise(resolve=>{release=resolve;}),'Buyer correction saved.');
 // The transport returns CASE_CHANGED before the buyer action finishes.
 Object.assign(state,{pending:false,error:true,conflict:true,message:'Buyer evidence changed during review.'});
 release();await action;
 assert.equal(reviewStates.get(selectedKey),state);
 assert.match($('support-reading-feedback').textContent,/changed/);
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'false');
 assert.equal($('support-review-button').disabled,false);
 assert.equal($('support-review-button').textContent,'Review current support');
 // A later deliberate action can clear the already-shown conflict.
 await runAction(async()=>{},'Handling saved.');
 assert.equal(reviewStates.has(selectedKey),false);
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'false');
 // An explicitly superseded/cleared review must not leave a stale ARIA marker.
 reviewStates.clear();$('support-reading-feedback').attrs['aria-busy']='true';renderReviewState();
 assert.equal($('support-reading-feedback').textContent,'');
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'false');
 // A still-running request remains pending; an obsolete success is cleared.
 const pending={pending:true,support:true,message:'Reading support…'};reviewStates.set(selectedKey,pending);
 await runAction(async()=>{},'Note saved.');
 assert.equal(reviewStates.get(selectedKey),pending);
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'true');
 assert.equal($('support-review-button').disabled,true);
 Object.assign(pending,{pending:false,message:'Support reading ready.'});
 await runAction(async()=>{},'Buyer correction saved.');
 assert.equal(reviewStates.has(selectedKey),false);
 assert.equal($('support-reading-feedback').attrs['aria-busy'],'false');
 console.log(JSON.stringify({fast_conflict_preserved:true,orphan_busy_cleared:true,pending_retained:true,obsolete_success_cleared:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())
