// spool web · js/core/store.js
// 页面状态只活在这一个对象里。feature 通过 ctx.store 读写，不再各写一份 let。

const WAIT_PREFIX = '模型生成中，已等待';
const RUN_MARK = '运行中';
const LOCK = 'lock';

function createStore(initial) {
  const state = {};
  const seed = initial || {};
  const seedKeys = Object.keys(seed);
  for (let index = 0; index < seedKeys.length; index += 1) {
    state[seedKeys[index]] = seed[seedKeys[index]];
  }
  const listeners = [];
  function emit(keys) {
    const partial = {};
    for (let index = 0; index < keys.length; index += 1) {
      partial[keys[index]] = state[keys[index]];
    }
    for (let index = 0; index < listeners.length; index += 1) {
      listeners[index](keys, partial);
    }
  }
  return {
    get: function (key) { return state[key]; },
    set: function (key, value) {
      if (!Object.prototype.hasOwnProperty.call(state, key)) { return value; }
      if (Object.is(state[key], value)) { return value; }
      state[key] = value;
      emit([key]);
      return value;
    },
    patch: function (partial) {
      const incoming = Object.keys(partial || {});
      const keys = [];
      for (let index = 0; index < incoming.length; index += 1) {
        const key = incoming[index];
        if (!Object.prototype.hasOwnProperty.call(state, key)) { continue; }
        if (Object.is(state[key], partial[key])) { continue; }
        state[key] = partial[key];
        keys.push(key);
      }
      if (keys.length) { emit(keys); }
      return state;
    },
    update: function (key, fn) {
      if (!Object.prototype.hasOwnProperty.call(state, key)) { return undefined; }
      state[key] = fn(state[key]);
      emit([key]);
      return state[key];
    },
    subscribe: function (fn) {
      listeners.push(fn);
      return function () {
        const at = listeners.indexOf(fn);
        if (at >= 0) { listeners.splice(at, 1); }
      };
    },
  };
}

const INITIAL_STATE = {
  running: false,
  decided: false,
  stopping: false,
  stoppedRun: false,
  readonly: false,
  session: 'cli',
  pendings: [],
  drawn: 0,
  busy: false,
  bound: '',
  sidebarSig: '',
  chosen: {root: '', session: ''},
  armed: '',
  lastState: {},
  thinking: false,
  card: null,
  cardAsking: false,
  cardApply: null,
  cardReject: null,
  cardSig: '',
  healthView: {cls: 'warn', label: '探活中', lines: [], raw: ''},
  healthInfo: {},
  pairingRows: [],
  elsewhereRows: [],
  readOnlyNote: '',
  pinnedNote: '',
  pinned: false,
  targetNotes: [],
  targetMissing: false,
  statusOpen: false,
  settingsOpen: false,
  tasksOpen: false,
  renameOpen: false,
  lastSettingsFocus: null,
  drafts: {},
};
const STATE_KEYS = Object.keys(INITIAL_STATE);
const store = createStore(INITIAL_STATE);
export { createStore, store, STATE_KEYS };
