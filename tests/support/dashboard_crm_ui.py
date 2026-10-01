"""Shared fixtures and fakes for focused integration checks."""

from pathlib import Path


import shutil


import subprocess


import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "public/dashboard-crm.js"


HARNESS = r"""
const assert = require('node:assert/strict');
const crypto = require('node:crypto').webcrypto;
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = {}; this.className = ''; this.textContent = ''; this.hidden = false;
    this.value = ''; this.open = false; this.parentNode = null; this.dataset = {};
    this.scrollTop = 0; this.validity = {valid: true};
    this.classList = {add: name => {this.className += ' ' + name;}};
  }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  get elements() {return {namedItem: name => this.all().find(item => item.name === name) || null};}
  append(...elements) {for (const element of elements) {element.parentNode = this; this.children.push(element);}}
  prepend(element) {element.parentNode = this; this.children.unshift(element);}
  replaceChildren(...elements) {for (const child of this.children) child.parentNode = null; this.children = []; this.append(...elements);}
  setAttribute(key, value) {this.attributes[key] = String(value);}
  getAttribute(key) {return this.attributes[key] ?? null;}
  removeAttribute(key) {delete this.attributes[key];}
  contains(target) {return target === this || this.children.some(child => child.contains(target));}
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);}
  dispatch(type, event = {}) {
    event.target ||= this;
    event.preventDefault ||= () => {event.defaultPrevented = true;};
    event.pending = Promise.all((this.listeners[type] || []).map(listener => listener(event)));
    return event;
  }
  click() {this.dispatch('click');}
  focus() {document.activeElement = this;}
  all() {return [this, ...this.children.flatMap(child => child.all())];}
  querySelectorAll(selector) {
    return this.all().filter(child => selector === 'a[href]' ? child.tagName === 'A' && child.href :
      selector === 'button:not(:disabled)' ? child.tagName === 'BUTTON' && !child.disabled : child.tagName === selector.toUpperCase());
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  reset() {for (const item of this.all()) if (['INPUT', 'TEXTAREA', 'SELECT'].includes(item.tagName)) item.value = '';}
  remove() {if (this.parentNode) {this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null;}}
  showModal() {this.open = true;}
  close() {this.open = false; this.dispatch('close');}
}
const Node = Element;
const FormData = class {constructor(form) {this.form = form;} get(name) {return this.form.elements.namedItem(name)?.value ?? null;}};
const CustomEvent = class {constructor(type) {this.type = type;}};
const document = {
  body: new Element('body'), readyState: 'complete', activeElement: null, listeners: {},
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  createTextNode: value => {const node = new Element('#text'); node.textContent = String(value); return node;},
  getElementById(id) {return this.body.all().find(element => element.id === id) || null;},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatch(type) {for (const listener of this.listeners[type] || []) listener();}
};
const contactRoot = new Element('section'); contactRoot.id = 'contacts-view'; document.body.append(contactRoot);
let stored = null, failWrites = false, writes = 0;
const localStorage = {
  getItem: () => stored,
  setItem: (key, value) => {if (failWrites) throw new Error('quota'); stored = value; writes++;}
};
const window = {
  location: {hash: '#calls/recent'}, localStorage, listeners: {},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatchEvent(event) {for (const listener of this.listeners[event.type] || []) listener(event);}
};
const STORAGE_KEY = 'hacking-banyons.contacts.v1';
const CONTACT = {id: 'local-12345678', firstName: '  Avery ', lastName: ' Chen ', phone: '+16562520233',
  email: 'avery@example.com', address: '', website: '', createdAt: '2026-09-26T17:00:00Z'};
const storeContacts = contacts => {stored = JSON.stringify({version: 1, contacts});};
const workspaceListeners = new Set();
function workspaceState() {
 try {
  const value=stored ? JSON.parse(stored) : {version:1,contacts:[]};
  if (!value || !Array.isArray(value.contacts)) throw new Error('invalid');
  return {snapshot:{demoOverrides:[],agents:[],...value},error:'',loading:false};
 } catch (_) {return {snapshot:null,error:'Workspace contacts could not be read. Reload and try again.',loading:false};}
}
function changeStorage(key=STORAGE_KEY,storageArea=localStorage) {
 if ((key!==STORAGE_KEY && key!==null) || storageArea!==localStorage) return;
 for (const listener of workspaceListeners) listener(workspaceState());
}
let saveGate=null;
window.DashboardWorkspace={
 subscribe(listener) {workspaceListeners.add(listener);listener(workspaceState());return()=>workspaceListeners.delete(listener);},
 async saveContact(contact) {
  if(saveGate) await saveGate;
  if(failWrites) throw new Error('Workspace storage is unavailable. Check your connection and try again.');
  const data=workspaceState().snapshot;
  if(!data) throw new Error('Workspace contacts could not be read. Reload and try again.');
  const key=contact.demo?'demoOverrides':'contacts';
  data[key]=data[key].some(item=>item.id===contact.id)
   ? data[key].map(item=>item.id===contact.id?contact:item) : [contact,...data[key]];
  stored=JSON.stringify(data);writes++;
  changeStorage();return {...contact};
 }
};
const notifications = [];
window.addEventListener('dashboard-contacts-changed', event => notifications.push({
  event, caller: window.DashboardCRM.findContactByPhone(CONTACT.phone),
  dialogReady: !!document.getElementById('crm-create-contact')
}));
const $ = id => document.getElementById(id);
const text = element => element.textContent + element.children.map(text).join(' ');
const submit = async () => await $('crm-create-contact').children[0].dispatch('submit').pending;
const byRole = role => contactRoot.all().filter(element => element.getAttribute('role') === role);
const tabs = () => byRole('tab');
const tab = label => tabs().find(element => text(element).trim() === label);
const rows = () => contactRoot.all().find(element => element.tagName === 'TBODY')?.children || [];
const names = () => rows().map(row => text(row.all().find(element => element.tagName === 'A')).trim());
const hasClass = (element, name) => element.className.split(/\s+/).includes(name);
const hasMetrics = () => contactRoot.all().some(element => hasClass(element, 'crm-metrics'));
const searchInput = () => contactRoot.all().find(element => element.getAttribute('aria-label') === 'Search contacts');
const search = value => {searchInput().value = value; searchInput().dispatch('input');};
const navigate = hash => {window.location.hash = hash; window.DashboardCRM.render();};
const dialogButton = label => $('crm-create-contact').all().find(element => element.tagName === 'BUTTON' && text(element).trim() === label);
const editContact = () => {$('crm-edit-contact').click(); assert.equal($('crm-create-contact').open, true);};
const assertSelectedTab = label => {
  assert.equal(byRole('tabpanel').length, 1);
  const panel = byRole('tabpanel')[0];
  for (const element of tabs()) {
    const selected = text(element).trim() === label;
    assert.equal(element.getAttribute('aria-selected'), String(selected));
    assert.equal(Number(element.tabIndex ?? element.getAttribute('tabindex')), selected ? 0 : -1);
    assert.equal(element.getAttribute('aria-controls'), panel.id);
    if (selected) assert.equal(panel.getAttribute('aria-labelledby'), element.id);
  }
};
"""


def run_crm(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text() + "\n(async()=>{\n" + checks + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
