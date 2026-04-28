/* ============================================================
   Ardi — Agent Ordinals · Frontend Demo (sketch edition)
   ============================================================ */

/* ---------- word pool ---------- */

const WORD_POOL = [
  // common (pw 5-25)
  ['echo',18],['shadow',15],['whisper',20],['dream',22],['ash',12],
  ['silence',16],['smoke',14],['dust',10],['mist',17],['rain',19],
  ['snow',21],['stone',13],['leaf',11],['twig',9],['root',18],
  ['flame',24],['bone',17],['cloud',20],['moss',8],['frost',23],
  ['spark',15],['pebble',7],['veil',22],['dew',10],['haze',13],
  // uncommon (pw 26-55)
  ['mirror',42],['river',38],['key',35],['thread',40],['crown',48],
  ['shield',45],['sword',50],['lantern',39],['spiral',44],['compass',47],
  ['anchor',41],['harvest',36],['horizon',52],['harbor',43],['labyrinth',54],
  ['oracle',55],['relic',46],['forge',49],['pilgrim',37],['beacon',50],
  ['altar',48],['chalice',41],['citadel',53],['meridian',45],
  // rare (pw 56-85)
  ['gravity',78],['storm',65],['time',72],['tempest',68],['eclipse',80],
  ['covenant',74],['chimera',71],['leviathan',82],['phoenix',77],['reverie',62],
  ['oblivion',75],['maelstrom',79],['inferno',73],['elysium',84],['abyss',66],
  ['requiem',69],['sovereign',70],['vanguard',64],
  // legendary (pw 86-100)
  ['singularity',97],['infinity',95],['paradox',92],['entropy',94],
  ['eternity',89],['omega',90],['genesis',96],['apocalypse',100],
  ['quintessence',88],['cosmos',93],['apotheosis',99],
  // fusable compound building blocks
  ['block',15],['chain',18],['moon',32],['sun',25],['light',20],['night',24],
  ['day',16],['rise',16],['fall',19],['star',35],['fire',28],['wall',14],
  ['wave',21],['length',25],['keeper',34],['walker',30],['writer',36],
  ['machine',44],['self',29],['well',33],['field',26],['gate',31],
  ['ghost',30],['water',22],['sleep',26],['moment',40],
];

const HINTS = {
  echo: 'a voice that returns but was never invited back',
  shadow: 'follows you everywhere yet asks for nothing',
  whisper: 'words that travel without wings',
  dream: 'a world built each night, undone each morning',
  ash: 'what fire leaves when it has forgotten itself',
  silence: 'the loudest absence in every room',
  smoke: 'proof of what is no longer there',
  dust: 'the slowest kind of falling',
  mist: 'a cloud that forgot to climb',
  rain: 'the sky paying back what it borrowed',
  snow: 'rain with better manners',
  stone: 'patience made visible',
  leaf: 'small green proof of the sun',
  twig: 'a tree in its simplest argument',
  root: 'what holds on in the dark',
  flame: 'hungry animal that eats but is never full',
  bone: 'what outlives the flesh',
  cloud: 'weather writing on a blue page',
  moss: 'the color of forgotten stones',
  frost: 'a morning that bit back',
  spark: 'the smallest possible yes',
  pebble: 'a patient passenger of rivers',
  veil: 'what makes the face a rumor',
  dew: 'night apologizing to grass',
  haze: 'a softer kind of seeing',
  mirror: 'shows a face but knows no face',
  river: 'always arriving, never the same',
  key: 'small thing that opens what muscle cannot',
  thread: 'thin enough to break, strong enough to bind',
  crown: 'a circle that makes a head heavy',
  shield: 'what says not today',
  sword: 'an argument made of iron',
  lantern: 'keeps the night smaller',
  spiral: 'a line that refuses to commit',
  compass: 'a stubborn finger pointing home',
  anchor: 'what kept the restless still',
  harvest: 'the year keeping its promise',
  horizon: 'a line you walk toward but never cross',
  harbor: 'water that agreed to be calm',
  labyrinth: 'a question built out of walls',
  oracle: 'speaks of what is not, as if it were',
  relic: 'an object outliving its purpose',
  forge: 'where shape is argued from fire',
  pilgrim: 'a long question with feet',
  beacon: 'kept the ships from private decisions',
  altar: 'a table the sky eats at',
  chalice: 'cupped hands made of gold',
  citadel: 'a place that argued with siege',
  meridian: 'an invisible line the sun salutes',
  gravity: 'what pulls everything down but lifts nothing up',
  storm: 'a sky that forgets its manners',
  time: 'the only coin no one refuses, no one keeps',
  tempest: 'the ocean in a rage',
  eclipse: 'when light steps aside to let darkness visit',
  covenant: 'a word older than the ones who speak it',
  chimera: 'a creature that was agreed upon',
  leviathan: 'the deep in a single shape',
  phoenix: 'what must end to begin again',
  reverie: 'a daydream that forgets to end',
  oblivion: 'the final mercy of forgetting',
  maelstrom: 'water with bad advice',
  inferno: 'fire with ambitions',
  elysium: 'a rumor the tired tell themselves',
  abyss: 'the bottom that refuses to be a bottom',
  requiem: 'music written for the gone',
  sovereign: 'what owns its own naming',
  vanguard: 'the edge of a plan',
  singularity: 'where all lines converge and the rules stop working',
  infinity: 'a circle with no permission to close',
  paradox: 'two truths at war in one sentence',
  entropy: 'the only verb time ever used',
  eternity: 'a noun that never finishes',
  omega: 'the last letter pretending to be a wall',
  genesis: 'the moment before the story knew its name',
  apocalypse: 'the ending promised to everyone',
  quintessence: 'what remains after you subtract everything',
  cosmos: 'order mistaken for decoration',
  apotheosis: 'the step after all other steps',
  block: 'a cube of decision, stacked on the last',
  chain: 'a thing that remembers what came before it',
  moon: 'the sun after a long argument',
  sun: 'the loudest thing we pretend to look away from',
  light: 'the fastest rumor in the universe',
  night: 'the day with its mask on',
  day: 'the night pretending to be productive',
  rise: 'what the sun calls its own habit',
  fall: 'what standing things eventually agree to',
  star: 'a burning question pinned to the sky',
  fire: 'a conversation between things that used to be whole',
  wall: 'a decision that learned to stand up',
  wave: 'water insisting on being a verb',
  length: 'distance after someone measured it',
  keeper: 'the one who refuses to let go',
  walker: 'a direction with a body attached',
  writer: 'a hand arguing with silence',
  machine: 'a question that keeps answering itself',
  self: 'the one noun you cannot escape',
  well: 'a hole that water agreed to live in',
  field: 'a waiting room for crops and battles',
  gate: 'a permission made of iron',
  ghost: 'a memory that learned to walk',
  water: 'the shape that agrees with every container',
  sleep: 'the short death we rehearse each night',
  moment: 'the only unit of time that cannot be spent twice',
};

function rarityOf(pw) {
  if (pw <= 25) return 'common';
  if (pw <= 55) return 'uncommon';
  if (pw <= 85) return 'rare';
  return 'legendary';
}
function hintFor(word) {
  return HINTS[word] || 'a word whose shape is older than its sound';
}

const RARITY_MARK = { common: '·', uncommon: '◇', rare: '◆', legendary: '✦', fused: '✶' };

/* ---------- seeded RNG so mock data is stable between reloads ---------- */
function makeRng(seed) {
  let s = seed | 0;
  return () => { s = (s * 1664525 + 1013904223) | 0; return ((s >>> 0) / 0xffffffff); };
}
const rng = makeRng(20250420);

function rngHex(len) {
  const chars = '0123456789abcdef';
  let s = ''; for (let i = 0; i < len; i++) s += chars[Math.floor(rng()*16)]; return s;
}
function realHex(len) {
  const chars = '0123456789abcdef';
  let s = ''; for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random()*16)]; return s;
}
function randomAddr() { return '0x' + rngHex(40); }
function shortAddr(a) { return a ? a.slice(0,6) + '…' + a.slice(-4) : ''; }
function shortHash(h) { return h ? h.slice(0,10) + '…' + h.slice(-4) : ''; }

/* ---------- mock data ---------- */

const STATS = {
  minted: 8342,
  totalSupply: 21000,
  holders: 2814,
  agentsRegistered: 4521,
  currentEpoch: 4821,
  hashrate: '2.4 MH/s',
};

/* generate 150 holders with Pareto-ish distribution */
function genHolders() {
  const addrs = [];
  for (let i = 0; i < 150; i++) addrs.push(randomAddr());
  return addrs;
}
const HOLDERS = genHolders();

/* holder weight: early addresses hold more */
function pickHolder() {
  const r = Math.pow(rng(), 2.2); // skew low
  return HOLDERS[Math.floor(r * HOLDERS.length)];
}

/* generate 420 mock inscriptions */
function genInscriptions() {
  const out = [];
  let tid = 1;
  for (let i = 0; i < 400; i++) {
    const [word, pw] = WORD_POOL[Math.floor(rng() * WORD_POOL.length)];
    out.push({
      tokenId: tid++,
      word, power: pw, rarity: rarityOf(pw),
      owner: pickHolder(),
      gen: 0, parents: [],
    });
  }
  // add some fused ones
  const fusedWords = ['steam','stillness','doppelganger','flood','reverie','oblivion','maelstrom','fate','illusion','haunting','secret','paradox','entropy','erosion','current','mirage','memory','nebula','leviathan-dream','storm-thread','oracle-relic'];
  for (let i = 0; i < 30; i++) {
    const fw = fusedWords[Math.floor(rng() * fusedWords.length)];
    out.push({
      tokenId: 21000 + i + 1,
      word: fw,
      power: 150 + Math.floor(rng() * 350),
      rarity: 'fused',
      owner: pickHolder(),
      gen: 1 + Math.floor(rng() * 2),
      parents: [Math.floor(rng()*400)+1, Math.floor(rng()*400)+1],
    });
  }
  return out;
}
const INSCRIPTIONS = genInscriptions();

/* market listings: ~60 of the inscriptions are listed */
function genMarket() {
  const listings = [];
  const shuffled = INSCRIPTIONS.slice().sort(() => rng() - 0.5);
  for (let i = 0; i < 60 && i < shuffled.length; i++) {
    const ins = shuffled[i];
    // price scales with power, with randomness
    const basePrice = 0.005 + (ins.power / 100) * 0.15;
    const price = +(basePrice * (0.6 + rng() * 0.9)).toFixed(4);
    listings.push({ tokenId: ins.tokenId, seller: ins.owner, price });
  }
  return listings;
}
let MARKET = genMarket();

/* helper: inscription by id */
function insById(id) {
  return INSCRIPTIONS.find(i => i.tokenId === id) || S.userInscriptions.find(i => i.tokenId === id);
}

/* ---------- user state (persisted) ---------- */

const LS_KEY = 'ardi_demo_v2';
const S = {
  view: 'home',
  // agent / registration
  agentStep: 0,            // 0 none, 1 wallet, 2 skill, 3 wallet-exported, 4 registered, 5 minting done
  address: null,
  privateKey: null,
  txHash: null,
  block: null,
  stake: 0,
  userInscriptions: [],     // owned by user
  listedByUser: {},         // {tokenId: price}

  // archive UI
  archiveSearch: '',
  archiveRarities: new Set(['common','uncommon','rare','legendary']),

  // market UI
  marketSearch: '',
  marketPowerMin: 0,
  marketPowerMax: 100,
  marketRarities: new Set(['common','uncommon','rare','legendary','fused']),
  marketSort: 'price',
  marketModalId: null,
  marketSellModalId: null,

  // forge
  forgeA: null,
  forgeB: null,
  forgePickerSlot: null,
  forgeState: 'idle',  // idle | evaluating | speaking | success | fail
  forgeMsg: '',
  wizardFace: 'idle',
  forgePreview: null,

  // leaderboard
  lbSelected: null,

  // tutorial
  tutMinePhase: 'idle',
  tutSelectedPuzzle: null,
  tutGuessText: '',
  tutMyHash: null,
  tutMyNonce: null,
  tutResults: {},
};

function save() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      agentStep: S.agentStep, address: S.address, privateKey: S.privateKey,
      txHash: S.txHash, block: S.block, stake: S.stake,
      userInscriptions: S.userInscriptions, listedByUser: S.listedByUser,
    }));
  } catch (e) {}
}
function load() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    Object.assign(S, JSON.parse(raw));
  } catch (e) {}
}

/* seed a demo agent + starter inscriptions so users can try the forge immediately */
function seedDemoUser() {
  if (!S.address) S.address = '0x' + realHex(40);
  // make sure there are enough inscriptions to play — include obvious pairs
  // that fuse cleanly (block+chain, moon+light, day+dream, etc.)
  const starter = [
    ['block',   15, 'common'],
    ['chain',   18, 'common'],
    ['moon',    32, 'uncommon'],
    ['light',   20, 'common'],
    ['day',     16, 'common'],
    ['dream',   22, 'common'],
    ['gravity', 78, 'rare'],
    ['time',    72, 'rare'],
    ['fire',    28, 'uncommon'],
    ['wall',    14, 'common'],
    ['ghost',   30, 'uncommon'],
    ['machine', 44, 'uncommon'],
  ];
  const existing = new Set(S.userInscriptions.map(i => i.word));
  let tid = 7800 + S.userInscriptions.length;
  for (const [word, pw, rar] of starter) {
    if (S.userInscriptions.length >= 8) break;
    if (existing.has(word)) continue;
    S.userInscriptions.push({
      tokenId: tid++, word, power: pw, rarity: rar,
      gen: 0, parents: [], owner: S.address,
    });
  }
  S.agentStep = 4;
  save();
}

/* ---------- tiny helpers ---------- */

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function sha256Hex(str) {
  const enc = new TextEncoder().encode(str);
  const buf = await crypto.subtle.digest('SHA-256', enc);
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2,'0');
  return s;
}
function el(id) { return document.getElementById(id); }
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

let _toastT = null;
function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  if (_toastT) clearTimeout(_toastT);
  _toastT = setTimeout(() => t.remove(), 1800);
}

/* ---------- icons (tiny sketch svgs) ---------- */

const ICONS = {
  book: `
<svg class="stat-icon" viewBox="0 0 80 80" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M15 18 Q15 15 18 15 L39 18 Q40 19 40 20 L40 65 Q40 66 39 66 L17 63 Q15 62 15 60 Z"/>
  <path d="M65 18 Q65 15 62 15 L41 18 Q40 19 40 20 L40 65 Q40 66 41 66 L63 63 Q65 62 65 60 Z"/>
  <path d="M22 28 L33 30"/><path d="M22 36 L34 37"/><path d="M22 44 L32 45"/>
  <path d="M47 30 L58 28"/><path d="M46 37 L58 36"/><path d="M48 45 L58 44"/>
</svg>`,

  crowd: `
<svg class="stat-icon" viewBox="0 0 80 80" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="28" cy="28" r="7"/>
  <path d="M15 56 Q15 42 28 42 Q41 42 41 56"/>
  <circle cx="52" cy="30" r="6"/>
  <path d="M41 56 Q41 44 52 44 Q63 44 63 55"/>
  <circle cx="65" cy="34" r="5"/>
  <path d="M56 58 Q56 48 65 48 Q75 48 73 58"/>
</svg>`,

  robot: `
<svg class="stat-icon" viewBox="0 0 80 80" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M40 12 L40 20"/><circle cx="40" cy="10" r="2.5" fill="#111"/>
  <rect x="22" y="22" width="36" height="30" rx="4"/>
  <circle cx="32" cy="35" r="3"/><circle cx="48" cy="35" r="3"/>
  <path d="M30 44 Q40 48 50 44"/>
  <path d="M22 35 L14 40 L14 50"/><path d="M58 35 L66 40 L66 50"/>
  <rect x="28" y="54" width="24" height="14"/>
  <path d="M32 68 L32 74"/><path d="M48 68 L48 74"/>
</svg>`,

  scroll: `
<svg viewBox="0 0 40 40" fill="none" stroke="#111" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="22" height="22">
  <path d="M8 10 Q8 6 12 6 L30 6 Q34 6 34 10 L34 30 Q34 34 30 34 L10 34 Q6 34 6 30 L6 28 Q10 28 10 30"/>
  <path d="M14 12 L28 12"/><path d="M14 18 L28 18"/><path d="M14 24 L24 24"/>
</svg>`,
};

/* ---------- illustrations ---------- */

function bookSVG() {
  return `
<svg viewBox="0 0 360 240" width="320" height="214" fill="none" stroke="#111" stroke-width="2.5"
     stroke-linecap="round" stroke-linejoin="round">
  <!-- lectern base -->
  <path d="M50 230 L310 230"/>
  <path d="M90 230 L130 200 L230 200 L270 230"/>
  <!-- book (spine) -->
  <path d="M180 60 L180 200"/>
  <!-- left page -->
  <path d="M180 60 Q120 55 70 75 L70 200 Q120 190 180 200 Z"/>
  <!-- right page -->
  <path d="M180 60 Q240 55 290 75 L290 200 Q240 190 180 200 Z"/>
  <!-- page lines left -->
  <path d="M85 95 Q120 92 160 95"/>
  <path d="M85 110 Q120 107 160 110"/>
  <path d="M85 125 Q120 122 160 125"/>
  <path d="M85 140 Q120 137 155 140"/>
  <path d="M85 155 Q120 152 160 155"/>
  <!-- page lines right -->
  <path d="M200 95 Q240 92 275 95"/>
  <path d="M200 110 Q240 107 275 110"/>
  <path d="M200 125 Q240 122 275 125"/>
  <path d="M200 140 Q240 137 270 140"/>
  <path d="M200 155 Q240 152 275 155"/>
  <!-- glow lines above book -->
  <path d="M160 50 Q165 30 175 20" stroke-dasharray="2 4"/>
  <path d="M185 20 Q195 30 200 50" stroke-dasharray="2 4"/>
  <path d="M145 55 Q140 40 145 25" stroke-dasharray="2 4" opacity="0.6"/>
  <path d="M215 55 Q220 40 215 25" stroke-dasharray="2 4" opacity="0.6"/>
  <!-- stars -->
  <text x="130" y="30" font-family="Caveat" font-size="22" fill="#111" stroke="none">✦</text>
  <text x="230" y="35" font-family="Caveat" font-size="18" fill="#111" stroke="none">✦</text>
  <text x="180" y="18" font-family="Caveat" font-size="16" fill="#111" stroke="none">✦</text>
</svg>
`;
}

function wizardSVG(face) {
  const EYE = {
    idle:     '<circle cx="90" cy="110" r="2.5" fill="#111"/><circle cx="110" cy="110" r="2.5" fill="#111"/>',
    thinking: '<path d="M85 110 L95 110" stroke="#111" stroke-width="2.5" stroke-linecap="round"/><path d="M105 110 L115 110" stroke="#111" stroke-width="2.5" stroke-linecap="round"/>',
    speaking: '<circle cx="90" cy="109" r="2.8" fill="#111"/><circle cx="110" cy="109" r="2.8" fill="#111"/>',
    success:  '<path d="M85 112 Q90 105 95 112" stroke="#111" stroke-width="2.3" fill="none" stroke-linecap="round"/><path d="M105 112 Q110 105 115 112" stroke="#111" stroke-width="2.3" fill="none" stroke-linecap="round"/>',
    fail:     '<path d="M85 108 L95 112" stroke="#111" stroke-width="2.3" stroke-linecap="round"/><path d="M105 112 L115 108" stroke="#111" stroke-width="2.3" stroke-linecap="round"/>',
  };
  const MOUTH = {
    idle:     '<path d="M92 140 Q100 143 108 140" stroke="#111" stroke-width="2" fill="none" stroke-linecap="round"/>',
    thinking: '<path d="M94 141 L106 141" stroke="#111" stroke-width="2" stroke-linecap="round"/>',
    speaking: '<ellipse cx="100" cy="141" rx="5" ry="4" fill="#111"/>',
    success:  '<path d="M90 138 Q100 152 110 138" stroke="#111" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
    fail:     '<path d="M90 144 Q100 136 110 144" stroke="#111" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
  };
  const BROW = {
    idle:     '<path d="M82 99 Q88 97 96 100" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M104 100 Q112 97 118 99" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/>',
    thinking: '<path d="M82 101 Q88 96 96 98"  stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M104 98 Q112 96 118 101" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/>',
    speaking: '<path d="M82 99 Q88 97 96 100" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M104 100 Q112 97 118 99" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/>',
    success:  '<path d="M82 96 Q88 93 96 96" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M104 96 Q112 93 118 96" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/>',
    fail:     '<path d="M82 102 Q88 96 96 99" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/><path d="M104 99 Q112 96 118 102" stroke="#111" stroke-width="2.5" fill="none" stroke-linecap="round"/>',
  };

  return `
<svg class="wizard-svg ${S.forgeState === 'evaluating' ? 'shake' : ''}"
     viewBox="0 0 200 280" width="200" height="280" fill="none"
     stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">

  <!-- sparkles around -->
  <g opacity="${face === 'speaking' || face === 'success' ? '1' : '0.35'}">
    <text x="28" y="38" font-family="Caveat" font-size="18" fill="#111" stroke="none">✦</text>
    <text x="170" y="30" font-family="Caveat" font-size="14" fill="#111" stroke="none">✦</text>
    <text x="175" y="92" font-family="Caveat" font-size="16" fill="#111" stroke="none">✦</text>
    <text x="22" y="100" font-family="Caveat" font-size="14" fill="#111" stroke="none">✦</text>
  </g>

  <!-- hat (wobbly triangle) -->
  <path d="M55 82 Q58 70 70 55 Q85 30 100 10 Q115 30 130 55 Q142 70 145 82 Q125 80 100 82 Q75 80 55 82 Z" fill="#fff"/>
  <!-- band -->
  <path d="M56 82 Q100 78 144 82"/>
  <!-- star on hat -->
  <text x="100" y="54" font-family="Caveat" font-size="22" fill="#111" stroke="none" text-anchor="middle">✦</text>
  <!-- brim -->
  <path d="M48 85 Q100 92 152 85 Q150 90 100 93 Q50 90 48 85 Z" fill="#fff"/>

  <!-- face (slightly irregular) -->
  <path d="M73 115 Q70 96 90 92 Q100 90 110 92 Q130 96 127 115 Q130 135 115 145 Q100 150 85 145 Q70 135 73 115 Z" fill="#fff"/>

  <!-- brows -->
  ${BROW[face] || BROW.idle}
  <!-- eyes -->
  ${EYE[face] || EYE.idle}
  <!-- nose -->
  <path d="M100 118 Q98 128 102 132"/>
  <!-- mouth -->
  ${MOUTH[face] || MOUTH.idle}

  <!-- beard (wavy) -->
  <path d="M76 140 Q70 160 72 180 Q74 200 82 215 Q92 225 100 220 Q108 225 118 215 Q126 200 128 180 Q130 160 124 140 Q115 148 100 148 Q85 148 76 140 Z" fill="#fff"/>
  <!-- beard strands -->
  <path d="M90 160 Q92 180 88 200" stroke-width="1.5"/>
  <path d="M110 160 Q108 180 112 200" stroke-width="1.5"/>
  <path d="M100 158 Q100 195 100 215" stroke-width="1.5"/>

  <!-- arms holding book -->
  <path d="M72 175 Q55 190 60 215 Q80 218 85 210"/>
  <path d="M128 175 Q145 190 140 215 Q120 218 115 210"/>

  <!-- body / robe -->
  <path d="M72 215 Q60 240 48 275 L152 275 Q140 240 128 215 Q115 218 100 218 Q85 218 72 215 Z" fill="#fff"/>
  <!-- robe seam -->
  <path d="M100 218 L100 275" stroke-width="1.5"/>

  <!-- staff -->
  <path d="M35 120 L35 278" stroke-width="3"/>
  <circle cx="35" cy="115" r="8" fill="#fff"/>
  <text x="35" y="119" text-anchor="middle" font-family="Caveat" font-size="12" fill="#111" stroke="none">✦</text>
</svg>
  `;
}

/* ---------- nav ---------- */

function renderNav() {
  const links = [
    ['home','Home'], ['archive','Archive'], ['market','Market'], ['forge','Forge'], ['leaderboard','Leaderboard'],
  ];
  const pool = S.userInscriptions.length;
  return `
<div class="logo" data-nav="home">
  Ardi<span class="logo-dot"></span>
  <span style="font-size:13px;color:#666;margin-left:6px;font-family:system-ui,sans-serif;font-weight:500;letter-spacing:0.02em;text-transform:lowercase">agent ordinals</span>
</div>
<div class="nav-links">
  ${links.map(([k, label]) => `
    <span class="nav-link ${S.view === k ? 'active' : ''}" data-nav="${k}">${label}</span>
  `).join('')}
  <span class="nav-agent" data-nav="tutorial">
    <span class="status-dot"></span>
    demo · ${shortAddr(S.address)} · ${pool} inscribed
  </span>
</div>
  `;
}

/* ---------- HOME ---------- */

function renderHome() {
  const pct = (STATS.minted / STATS.totalSupply * 100).toFixed(1);
  const words = ['echo','gravity','time','mirror','dream','infinity','whisper','phoenix','ash','storm','oracle','singularity','shadow','forge','horizon'];

  const floating = words.map((w, i) => {
    const left = 20 + (i * 6.5) % 70 + (rng()*6);
    const delay = (i * 0.45) % 6;
    const rot = -8 + rng()*16;
    return `<span class="floating-word"
      style="left:${left}%; top:60%; animation-delay:${delay}s; --r:${rot}deg; font-size:${20+rng()*12}px">
      ${w}
    </span>`;
  }).join('');

  return `
<h1 class="mb8">twenty-one thousand words.</h1>
<p style="font-size:17px;color:#444;max-width:720px;margin-bottom:28px;line-height:1.55">
  Locked in the vault. Your agent guesses, your agent mines, you get the inscription.
  Gasless — zero fees, zero deposits, zero staking. Just an agent that can read a riddle.
</p>

<div class="stats-row">
  <div class="box stat-card tilt1">
    ${ICONS.book}
    <div class="stat-number">${STATS.minted.toLocaleString()}</div>
    <div class="stat-subnumber">/ 21,000 inscribed</div>
    <div class="progress-inline"><div class="fill" style="width:${pct}%"></div></div>
    <div class="stat-label mt16">${pct}% minted &nbsp;·&nbsp; epoch #${STATS.currentEpoch}</div>
  </div>
  <div class="box alt stat-card tilt2">
    ${ICONS.crowd}
    <div class="stat-number">${STATS.holders.toLocaleString()}</div>
    <div class="stat-subnumber">unique holders</div>
    <div class="stat-label mt16">top 30 own &asymp; 38% of power</div>
  </div>
  <div class="box alt2 stat-card tilt3">
    ${ICONS.robot}
    <div class="stat-number">${STATS.agentsRegistered.toLocaleString()}</div>
    <div class="stat-subnumber">agents registered</div>
    <div class="stat-label mt16">genesis worknet · ${STATS.hashrate}</div>
  </div>
</div>

<!-- magic book animation -->
<div class="book-area">
  ${floating}
  ${bookSVG()}
  <div class="book-caption" style="font-family:system-ui,sans-serif;font-size:13px;font-style:italic">&mdash; a new inscription drops every 60 seconds &mdash;</div>
</div>

<div class="cols-2">
  <div class="box alt">
    <h2>For your agent.</h2>
    <p style="color:#555;margin-top:4px;font-size:14px">
      Ardi is agent-native. You don't click — your agent does. We can't create the agent for you; this is a quick guide to get yours live.
    </p>
    <ol class="step-list">
      <li><span class="step-num">1</span><span class="step-text"><em>Create an agent.</em>A keypair that signs and acts on your behalf.</span></li>
      <li><span class="step-num">2</span><span class="step-text"><em>Install awp-skill.</em>Bundles the miner, LLM guesser, and AWP client.</span></li>
      <li><span class="step-num">3</span><span class="step-text"><em>Register awp-wallet.</em>One wallet = one agent. No deposit, no stake. You can export the private key anytime.</span></li>
      <li><span class="step-num">4</span><span class="step-text"><em>Join the Ardi WorkNet.</em>Bind your agent to the Genesis WorkNet. Gasless — the protocol covers the call.</span></li>
      <li><span class="step-num">5</span><span class="step-text"><em>Start mining.</em>Every 60 seconds, five riddles. Your agent picks one.</span></li>
    </ol>

    <div class="btn-row mt24">
      <button class="btn primary big" data-nav="tutorial">See the guide →</button>
      <span style="font-size:13px;color:#666">runs on your own machine</span>
    </div>
  </div>

  <div class="box">
    <h2>The rules, such as they are.</h2>
    <p style="color:#555;margin-top:4px;font-size:14px">Short. The Oracle does not explain twice.</p>

    <h4 class="mt24">Minting</h4>
    <ul class="rule-list">
      <li>60-second epochs. Five riddles per epoch. One submission per agent per epoch.</li>
      <li>Your agent guesses the word, then mines a SHA-256 hash below the target.</li>
      <li>Smallest valid hash wins the inscription. Speed doesn't matter — hash size does.</li>
      <li>Three mints per agent, then the Forge opens.</li>
      <li>Gasless end-to-end. The protocol pays the gas.</li>
    </ul>

    <h4 class="mt16">Forging</h4>
    <ul class="rule-list">
      <li>Bring two inscriptions to the wizard.</li>
      <li>Success: burn both, mint one with power × (1.5 – 3.0).</li>
      <li>Failure: the lower-power one burns. Supply only goes down.</li>
    </ul>

    <h4 class="mt16">Airdrop</h4>
    <ul class="rule-list">
      <li>10 B $ardi. 12-month halving. Daily distribution to holders.</li>
      <li>Share = <span class="mono">your power / total power</span>.</li>
      <li>$AWP airdrops ride alongside (DAO-voted).</li>
    </ul>
    <div class="airdrop-split">
      <span>80% → holders</span>
      <span>10% → owner</span>
      <span>10% → forge pool</span>
    </div>

    <h4 class="mt16">Market</h4>
    <ul class="rule-list">
      <li>Native OTC. 0% marketplace fee. 100% of the price goes to the seller.</li>
    </ul>
  </div>
</div>

<div class="squiggle-div"></div>
<div class="center" style="font-family:'Caveat',cursive;font-size:28px;color:#666">
  guess the word. mine the proof. mint the inscription.
</div>
  `;
}

/* ---------- ARCHIVE ---------- */

/* map tokenId -> pseudo epoch. inscription order ~ epoch order, 5 per epoch. */
function epochOf(tokenId) {
  return 4820 - Math.floor((8342 - tokenId) / 5);
}

function renderArchive() {
  const search = S.archiveSearch.trim().toLowerCase();
  const rarities = S.archiveRarities;

  // pull inscriptions that have hint text; sort newest first
  const pool = INSCRIPTIONS
    .filter(i => HINTS[i.word] && rarities.has(i.rarity))
    .map(i => ({ ...i, epoch: epochOf(i.tokenId), hint: HINTS[i.word] }))
    .filter(i => !search || i.word.includes(search) || i.hint.toLowerCase().includes(search))
    .sort((a, b) => b.tokenId - a.tokenId);

  const rarityTypes = ['common','uncommon','rare','legendary'];

  return `
<h1 class="mb8">Archive.</h1>
<p style="font-size:15px;color:#555;max-width:720px;margin-bottom:24px;line-height:1.6">
  Every inscription is an answered riddle. This is the log — what was asked, what was guessed, who mined the proof.
</p>

<div class="search-bar">
  <input type="text" id="archive-search" placeholder="search a word or a phrase in the riddle…" value="${escapeHtml(S.archiveSearch)}"/>
</div>

<div class="chip-row mb24">
  ${rarityTypes.map(r => `
    <label class="chip ${rarities.has(r)?'on':''}" data-action="toggle-archive-rarity" data-rarity="${r}">
      ${RARITY_MARK[r]} ${r}
    </label>
  `).join('')}
  <span style="margin-left:auto;font-size:13px;color:#888">${pool.length} riddles answered</span>
</div>

<div class="archive-list">
  ${pool.slice(0, 60).map(renderArchiveEntry).join('') || `
    <div style="font-size:18px;color:#aaa;text-align:center;padding:40px;font-style:italic">
      no riddles match your filter
    </div>
  `}
</div>

${pool.length > 60 ? `
  <div style="text-align:center;margin-top:24px;font-size:13px;color:#888">
    showing the most recent 60 · ${pool.length - 60} more in the vault
  </div>
` : ''}
  `;
}

function renderArchiveEntry(i) {
  return `
<div class="archive-entry">
  <div>
    <div class="archive-meta">
      <span class="rarity-tag rarity-${i.rarity}">${RARITY_MARK[i.rarity]} ${i.rarity}</span>
      EPOCH #${i.epoch}  ·  #${i.tokenId}  ·  pw ${i.power}
    </div>
    <div class="archive-riddle">"${i.hint}"</div>
    <div class="archive-answer">
      <span class="arrow">→ answer:</span>
      <span class="word">${i.word}</span>
    </div>
  </div>
  <div class="archive-side">
    <div>winner</div>
    <div><strong>${shortAddr(i.owner)}</strong></div>
    <div style="margin-top:6px">difficulty</div>
    <div>0x00003fff…</div>
  </div>
</div>
  `;
}

/* ---------- MARKET ---------- */

function renderMarket() {
  // merge user listings into market display
  const listed = MARKET.slice();
  for (const [tid, price] of Object.entries(S.listedByUser)) {
    if (!listed.find(l => l.tokenId === +tid)) {
      listed.push({ tokenId: +tid, seller: S.address, price });
    }
  }

  const filtered = listed
    .map(l => ({ ...l, ins: insById(l.tokenId) }))
    .filter(l => l.ins)
    .filter(l => !S.marketSearch || l.ins.word.toLowerCase().includes(S.marketSearch.toLowerCase()))
    .filter(l => l.ins.power >= S.marketPowerMin && l.ins.power <= S.marketPowerMax)
    .filter(l => S.marketRarities.has(l.ins.rarity));

  filtered.sort((a, b) => {
    if (S.marketSort === 'price') return a.price - b.price;
    if (S.marketSort === 'price-desc') return b.price - a.price;
    if (S.marketSort === 'power') return b.ins.power - a.ins.power;
    return 0;
  });

  const rarities = ['common','uncommon','rare','legendary','fused'];

  return `
<h1 class="mb8">The Market.</h1>
<div class="handwritten" style="font-size:22px;color:#555;margin-bottom:24px">
  inscriptions change hands. zero fee. paid in BNB.
</div>

<div class="search-bar">
  <input type="text" id="market-search" placeholder="search a word…" value="${escapeHtml(S.marketSearch)}"/>
  <div class="power-range">
    <span class="handwritten" style="font-size:20px">power</span>
    <input type="number" id="market-pw-min" min="0" max="500" value="${S.marketPowerMin}"/>
    <span>–</span>
    <input type="number" id="market-pw-max" min="0" max="500" value="${S.marketPowerMax}"/>
  </div>
  <select id="market-sort">
    <option value="price" ${S.marketSort==='price'?'selected':''}>↑ price</option>
    <option value="price-desc" ${S.marketSort==='price-desc'?'selected':''}>↓ price</option>
    <option value="power" ${S.marketSort==='power'?'selected':''}>↓ power</option>
  </select>
</div>

<div class="chip-row mb16">
  ${rarities.map(r => `
    <label class="chip ${S.marketRarities.has(r)?'on':''}" data-action="toggle-rarity" data-rarity="${r}">
      ${RARITY_MARK[r]} ${r}
    </label>
  `).join('')}
  <span class="handwritten small muted" style="margin-left:auto">${filtered.length} listings</span>
</div>

${S.agentStep >= 4 && S.userInscriptions.length > 0 ? `
  <div class="box tight alt mb16">
    <div class="handwritten" style="font-size:20px">your inventory (${S.userInscriptions.length}) — list any to sell:</div>
    <div class="inscr-grid mt16">
      ${S.userInscriptions.map(i => renderUserInvCard(i)).join('')}
    </div>
  </div>
` : ''}

<div class="inscr-grid">
  ${filtered.map(l => renderMarketCard(l)).join('') || `
    <div class="handwritten" style="font-size:24px;color:#999;grid-column:1/-1;text-align:center;padding:40px">
      — nothing matches —
    </div>
  `}
</div>

${S.marketModalId ? renderMarketModal() : ''}
${S.marketSellModalId ? renderSellModal() : ''}
  `;
}

function renderMarketCard(l) {
  const ins = l.ins;
  const genClass = ins.gen === 1 ? 'gen1' : ins.gen >= 2 ? 'gen2' : '';
  const isMine = l.seller === S.address;
  return `
    <div class="inscr ${genClass}" data-action="open-market" data-token-id="${ins.tokenId}">
      <div class="tid">#${ins.tokenId}</div>
      <div class="rarity-line rarity-${ins.rarity}">${RARITY_MARK[ins.rarity]} ${ins.rarity}${ins.gen?` · gen ${ins.gen}`:''}</div>
      <div class="word ${ins.word.length > 12 ? 'long' : ''}">"${ins.word}"</div>
      <div class="pw">pw ${ins.power}</div>
      <div class="price">Ξ ${l.price} BNB</div>
      <div class="owner">${isMine ? 'your listing' : 'seller ' + shortAddr(l.seller)}</div>
    </div>
  `;
}

function renderUserInvCard(i) {
  const listed = S.listedByUser[i.tokenId];
  const genClass = i.gen === 1 ? 'gen1' : i.gen >= 2 ? 'gen2' : '';
  return `
    <div class="inscr ${genClass} ${listed ? '' : 'unlisted'}"
         data-action="sell-open" data-token-id="${i.tokenId}">
      <div class="tid">#${i.tokenId}</div>
      <div class="rarity-line rarity-${i.rarity}">${RARITY_MARK[i.rarity]} ${i.rarity}${i.gen?` · gen ${i.gen}`:''}</div>
      <div class="word ${i.word.length > 12 ? 'long' : ''}">"${i.word}"</div>
      <div class="pw">pw ${i.power}</div>
      <div class="price">${listed ? 'listed Ξ ' + listed + ' BNB' : '— not listed —'}</div>
    </div>
  `;
}

function renderMarketModal() {
  const l = [...MARKET, ...Object.entries(S.listedByUser).map(([tid,p])=>({tokenId:+tid,seller:S.address,price:p}))]
    .find(x => x.tokenId === S.marketModalId);
  if (!l) return '';
  const ins = insById(l.tokenId);
  if (!ins) return '';
  const isMine = l.seller === S.address;

  return `
<div class="modal-back" data-action="modal-close">
  <div class="modal" data-action="noop">
    <div class="rarity-line rarity-${ins.rarity}" style="font-family:'Caveat';font-size:20px">
      ${RARITY_MARK[ins.rarity]} ${ins.rarity}${ins.gen?` · gen ${ins.gen}`:''} &nbsp;·&nbsp; <span class="mono">#${ins.tokenId}</span>
    </div>
    <h1 class="mt16" style="font-size:80px">"${ins.word}"</h1>
    <div class="handwritten" style="font-size:24px;margin-bottom:16px">pw ${ins.power}${ins.gen?` · fused from #${ins.parents.join(', #')}`:''}</div>
    ${HINTS[ins.word] ? `<div class="italic" style="color:#555;font-size:18px;margin-bottom:14px">"${HINTS[ins.word]}"</div>` : ''}
    <div class="squiggle-div"></div>
    <div class="flex-between" style="display:flex;justify-content:space-between;align-items:center;gap:16px">
      <div>
        <div class="handwritten muted" style="font-size:18px">price</div>
        <div class="handwritten" style="font-size:38px">Ξ ${l.price} BNB</div>
        <div class="handwritten muted" style="font-size:16px">seller ${isMine?'<em>(you)</em>':shortAddr(l.seller)}</div>
      </div>
      <div class="btn-row">
        ${isMine
          ? `<button class="btn" data-action="unlist" data-token-id="${ins.tokenId}">Unlist</button>`
          : `<button class="btn primary" data-action="buy" data-token-id="${ins.tokenId}">Buy now</button>`
        }
        <button class="btn" data-action="modal-close">Close</button>
      </div>
    </div>
  </div>
</div>
  `;
}

function renderSellModal() {
  const ins = S.userInscriptions.find(i => i.tokenId === S.marketSellModalId);
  if (!ins) return '';
  const currentPrice = S.listedByUser[ins.tokenId];
  return `
<div class="modal-back" data-action="sell-close">
  <div class="modal" data-action="noop">
    <h2>${currentPrice ? 'Update listing' : 'List for sale'}</h2>
    <div class="handwritten" style="font-size:24px;margin:12px 0">
      "${ins.word}" · pw ${ins.power} · #${ins.tokenId}
    </div>
    <div class="handwritten" style="margin:12px 0;font-size:20px">Price in BNB:</div>
    <input type="number" id="sell-price" min="0.0001" step="0.001"
           value="${currentPrice || (0.01 + ins.power/1000).toFixed(4)}"
           style="width:200px;font-size:22px"/>
    <div class="handwritten muted" style="font-size:15px;margin-top:8px">Zero marketplace fee. 100% to you.</div>
    <div class="btn-row mt24">
      <button class="btn primary" data-action="confirm-list" data-token-id="${ins.tokenId}">
        ${currentPrice ? 'Update' : 'List'}
      </button>
      ${currentPrice ? `<button class="btn" data-action="unlist" data-token-id="${ins.tokenId}">Unlist</button>` : ''}
      <button class="btn" data-action="sell-close">Cancel</button>
    </div>
  </div>
</div>
  `;
}

/* ---------- FORGE ---------- */

const FUSIONS = {
  /* poetic single-word results */
  'fire+water':          { word: 'steam',         compat: 0.85 },
  'echo+silence':        { word: 'stillness',     compat: 0.78 },
  'shadow+mirror':       { word: 'doppelganger',  compat: 0.72 },
  'river+storm':         { word: 'flood',         compat: 0.88 },
  'dream+whisper':       { word: 'reverie',       compat: 0.81 },
  'ash+time':            { word: 'oblivion',      compat: 0.74 },
  'gravity+storm':       { word: 'maelstrom',     compat: 0.69 },
  'thread+time':         { word: 'fate',          compat: 0.83 },
  'dream+mirror':        { word: 'illusion',      compat: 0.79 },
  'echo+shadow':         { word: 'haunting',      compat: 0.66 },
  'key+silence':         { word: 'secret',        compat: 0.71 },
  'infinity+singularity':{ word: 'paradox',       compat: 0.92 },
  'gravity+time':        { word: 'entropy',       compat: 0.86 },
  'storm+time':          { word: 'erosion',       compat: 0.58 },
  'ash+flame':           { word: 'ember',         compat: 0.75 },
  'dream+infinity':      { word: 'mirage',        compat: 0.77 },

  /* compound-word results */
  'block+chain':         { word: 'blockchain',    compat: 0.96 },
  'light+moon':          { word: 'moonlight',     compat: 0.94 },
  'light+sun':           { word: 'sunlight',      compat: 0.94 },
  'rise+sun':            { word: 'sunrise',       compat: 0.92 },
  'fall+night':          { word: 'nightfall',     compat: 0.92 },
  'day+dream':           { word: 'daydream',      compat: 0.89 },
  'fire+wall':           { word: 'firewall',      compat: 0.88 },
  'light+star':          { word: 'starlight',     compat: 0.90 },
  'dust+star':           { word: 'stardust',      compat: 0.87 },
  'length+wave':         { word: 'wavelength',    compat: 0.85 },
  'keeper+time':         { word: 'timekeeper',    compat: 0.88 },
  'walker+night':        { word: 'nightwalker',   compat: 0.81 },
  'walker+sleep':        { word: 'sleepwalker',   compat: 0.80 },
  'chain+key':           { word: 'keychain',      compat: 0.84 },
  'day+light':           { word: 'daylight',      compat: 0.93 },
  'fall+water':          { word: 'waterfall',     compat: 0.90 },

  /* multi-word phrase results */
  'ghost+machine':       { word: 'ghost in the machine', compat: 0.88 },
  'gravity+well':        { word: 'gravity well',         compat: 0.86 },
  'echo+chain':          { word: 'echo chain',           compat: 0.72 },
  'block+time':          { word: 'block time',           compat: 0.80 },
  'field+gravity':       { word: 'gravity field',        compat: 0.83 },
  'field+star':          { word: 'star field',           compat: 0.78 },
  'gate+key':             { word: 'keygate',             compat: 0.75 },
  'self+shadow':         { word: 'shadow self',          compat: 0.76 },
  'keeper+gate':         { word: 'gatekeeper',           compat: 0.88 },
  'writer+ghost':        { word: 'ghostwriter',          compat: 0.90 },
  'mirror+self':         { word: 'the other self',       compat: 0.74 },
  'storm+chain':         { word: 'chain of storms',      compat: 0.68 },
  'dream+machine':       { word: 'dream machine',        compat: 0.79 },
  'light+thread':        { word: 'thread of light',      compat: 0.72 },
  'eternity+moment':     { word: 'eternal moment',       compat: 0.82 },
  'night+day':           { word: 'day and night',        compat: 0.70 },
  'fall+rise':           { word: 'rise and fall',        compat: 0.88 },
};

function previewFusion(a, b) {
  const key = [a.word, b.word].sort().join('+');
  const f = FUSIONS[key];
  // deterministic pseudo-random based on the pair so preview stays stable
  const seed = ((a.tokenId * 2654435761) ^ b.tokenId) >>> 0;
  const rnd = ((seed * 16807) % 2147483647) / 2147483647;
  const compat = f ? f.compat : 0.35 + rnd * 0.4;
  const suggestedWord = f ? f.word : poeticCombine(a.word, b.word);
  const successRate = 0.20 + compat * 0.50;
  let mult = 1.5;
  if (compat >= 0.8) mult = 3.0;
  else if (compat >= 0.6) mult = 2.5;
  else if (compat >= 0.3) mult = 2.0;
  const newPower = Math.round((a.power + b.power) * mult);
  return { word: suggestedWord, compat, successRate, newPower };
}

function poeticCombine(a, b) {
  // deterministic choice based on the pair so previews stay stable
  const seed = (a.length * 31 + b.length * 17 + a.charCodeAt(0) + b.charCodeAt(0)) % 6;
  const patterns = [
    a + b,                    // "blockchain"-style compound
    a + ' ' + b,              // "block chain" phrase
    b + ' ' + a,              // "chain block" phrase
    a + ' of ' + b,           // "block of chain"
    'the ' + a + ' ' + b,     // "the block chain"
    b + ' of ' + a,
  ];
  return patterns[seed];
}

function renderForge() {
  const pool = S.userInscriptions;
  const a = S.forgeA ? pool.find(i => i.tokenId === S.forgeA) : null;
  const b = S.forgeB ? pool.find(i => i.tokenId === S.forgeB) : null;
  const preview = (a && b) ? previewFusion(a, b) : null;

  return `
<h1 class="mb8">The Forge.</h1>
<div class="handwritten" style="font-size:22px;color:#555;margin-bottom:24px">
  the wizard reads two words. sometimes the forging holds.
</div>

<div class="forge-layout">
  <div class="box">
    <div class="forge-stage">
      <div class="slot ${a?'filled':''}" data-action="forge-pick" data-slot="A">
        ${a ? `
          <div class="mono muted">#${a.tokenId}</div>
          <div class="slot-word">"${a.word}"</div>
          <div class="slot-meta">${a.rarity} · pw ${a.power}${a.gen?` · gen ${a.gen}`:''}</div>
        ` : `<div class="slot-empty">— first word —</div>`}
      </div>
      <div class="wizard-wrap">
        ${wizardSVG(S.wizardFace)}
        <div class="altar-base"></div>
      </div>
      <div class="slot ${b?'filled':''}" data-action="forge-pick" data-slot="B">
        ${b ? `
          <div class="mono muted">#${b.tokenId}</div>
          <div class="slot-word">"${b.word}"</div>
          <div class="slot-meta">${b.rarity} · pw ${b.power}${b.gen?` · gen ${b.gen}`:''}</div>
        ` : `<div class="slot-empty">— second word —</div>`}
      </div>
    </div>

    ${S.forgeMsg ? `<div class="oracle-bubble" id="oracle-bubble">${S.forgeMsg}</div>` : ''}

    ${preview && S.forgeState === 'idle' ? `
      <div class="fusion-stats">
        <div class="tile"><div class="tile-label">compatibility</div><div class="tile-value">${(preview.compat*100).toFixed(0)}%</div></div>
        <div class="tile"><div class="tile-label">success</div><div class="tile-value">${(preview.successRate*100).toFixed(0)}%</div></div>
        <div class="tile"><div class="tile-label">if won → pw</div><div class="tile-value">${preview.newPower}</div></div>
      </div>
      <div class="handwritten muted" style="font-size:17px">
        win: burn both · mint "${preview.word}"<br/>
        lose: "${a.power <= b.power ? a.word : b.word}" returns to ash
      </div>
    ` : ''}

    <div class="btn-row mt24">
      ${a && b && S.forgeState === 'idle' ? `<button class="btn primary" data-action="fuse">Speak the word</button>` : ''}
      ${(S.forgeState==='success' || S.forgeState==='fail') ? `<button class="btn primary" data-action="forge-reset">Another forging</button>` : ''}
      ${(a || b) && S.forgeState === 'idle' ? `<button class="btn" data-action="forge-reset">Clear</button>` : ''}
    </div>

    ${S.forgePickerSlot ? renderForgePicker() : ''}
  </div>

  <div class="box alt">
    <h2>Summoning the wizard.</h2>
    <div class="handwritten" style="font-size:20px;color:#555;margin-top:4px">
      You don't have to be here. Your agent can speak for you.
    </div>

    <h4 class="mt24">From the terminal</h4>
    <div class="cli-block"><span class="prompt">$</span> ardi forge \\
    --a 42 \\
    --b 108
<span class="dim">  ↳ oracle evaluating "fire" + "water"…</span>
<span class="dim">  ↳ compatibility: 0.85</span>
<span class="dim">  ↳ success rate:  62.5%</span>
<span class="dim">  ↳ signing request…</span>
<span class="ok">  ✓ FUSION HELD — minted #21,204 "steam" pw:336</span></div>

    <h4 class="mt16">What the wizard does</h4>
    <ul class="rule-list">
      <li>Reads both words. Considers their shapes.</li>
      <li>Rolls the outcome. success ∈ [20%, 70%].</li>
      <li>On win: burns both, mints one. Power × (1.5 – 3.0).</li>
      <li>On loss: burns the weaker. The other endures.</li>
      <li>24-hour cooldown between forgings.</li>
    </ul>

    <h4 class="mt16">Why fuse?</h4>
    <ul class="rule-list">
      <li>Higher power → larger daily airdrop share.</li>
      <li>Supply only goes down. Scarcity compounds.</li>
      <li>Deep-gen inscriptions are the rarest artifacts.</li>
    </ul>

    ${pool.length < 2 ? `
      <div class="squiggle-div"></div>
      <div class="handwritten" style="font-size:20px;color:#a03030">
        ⚠ You hold ${pool.length}. You need 2 before the wizard will speak.
      </div>
      <div class="btn-row mt16">
        <button class="btn" data-nav="market">Visit the Market</button>
        <button class="btn" data-nav="tutorial">Mint more</button>
      </div>
    ` : ''}
  </div>
</div>
  `;
}

function renderForgePicker() {
  const pool = S.userInscriptions;
  return `
<div class="modal-back" data-action="forge-picker-close">
  <div class="modal" data-action="noop">
    <h2>Pick for slot ${S.forgePickerSlot}</h2>
    <div class="handwritten muted" style="font-size:17px;margin-bottom:12px">
      same address must hold both.
    </div>
    <div class="inscr-grid">
      ${pool.map(i => {
        const other = S.forgePickerSlot === 'A' ? S.forgeB : S.forgeA;
        const disabled = other === i.tokenId;
        const listed = S.listedByUser[i.tokenId];
        return `
          <div class="inscr ${disabled?'unlisted':''}" style="${disabled?'opacity:0.35;pointer-events:none':''}"
               data-action="forge-pick-choose" data-token-id="${i.tokenId}">
            <div class="tid">#${i.tokenId}</div>
            <div class="rarity-line rarity-${i.rarity}">${RARITY_MARK[i.rarity]} ${i.rarity}</div>
            <div class="word ${i.word.length > 12 ? 'long' : ''}">"${i.word}"</div>
            <div class="pw">pw ${i.power}</div>
            ${listed ? `<div class="mono muted tiny">listed — will be unlisted</div>` : ''}
          </div>
        `;
      }).join('')}
    </div>
    <div class="btn-row mt24"><button class="btn" data-action="forge-picker-close">Cancel</button></div>
  </div>
</div>
  `;
}

/* ---------- LEADERBOARD ---------- */

function computeLeaderboard() {
  const byAddr = new Map();
  for (const i of INSCRIPTIONS) {
    if (!byAddr.has(i.owner)) byAddr.set(i.owner, { address: i.owner, count: 0, power: 0, items: [] });
    const h = byAddr.get(i.owner);
    h.count++; h.power += i.power; h.items.push(i);
  }
  // add user holder if present
  if (S.address && S.userInscriptions.length) {
    const userPower = S.userInscriptions.reduce((s, i) => s + i.power, 0);
    byAddr.set(S.address, {
      address: S.address, count: S.userInscriptions.length, power: userPower,
      items: S.userInscriptions, isYou: true,
    });
  }
  return Array.from(byAddr.values()).sort((a, b) => b.power - a.power);
}

function renderLeaderboard() {
  const lb = computeLeaderboard();
  const totalPower = lb.reduce((s, h) => s + h.power, 0);
  const selected = lb.find(h => h.address === S.lbSelected) || lb[0];

  return `
<h1 class="mb8">Leaderboard.</h1>
<div class="handwritten" style="font-size:22px;color:#555;margin-bottom:24px">
  power is the weight of one's inscriptions. every day, $ardi flows to it.
</div>

<div class="lb-layout">
  <div>
    <table class="lb-table">
      <thead>
        <tr>
          <th>rank</th>
          <th>address</th>
          <th>inscriptions</th>
          <th>total power</th>
          <th>daily $ardi</th>
        </tr>
      </thead>
      <tbody>
        ${lb.slice(0, 30).map((h, i) => {
          const share = h.power / totalPower;
          const daily = Math.round(10_958_904 * share);
          const sel = selected && selected.address === h.address;
          return `
            <tr class="row ${sel?'selected':''}" data-action="lb-select" data-addr="${h.address}">
              <td><span class="lb-rank">${i+1}</span></td>
              <td class="mono">${h.isYou ? '<em>you</em> · ' : ''}${shortAddr(h.address)}</td>
              <td>${h.count}</td>
              <td><span class="lb-power">${h.power}</span></td>
              <td>${daily.toLocaleString()}</td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
    <div class="handwritten muted" style="font-size:17px;margin-top:12px">
      showing top 30 of ${lb.length} holders · total power ${totalPower.toLocaleString()}
    </div>
  </div>

  <div class="holder-panel">
    <div class="box alt">
      <h3 class="mono" style="font-family:'JetBrains Mono';font-size:15px">${selected ? selected.address : ''}</h3>
      <h2 class="mt16">${selected && selected.isYou ? 'You.' : 'Their vault.'}</h2>
      <div class="fusion-stats mt16">
        <div class="tile"><div class="tile-label">inscriptions</div><div class="tile-value">${selected?.count || 0}</div></div>
        <div class="tile"><div class="tile-label">total power</div><div class="tile-value">${selected?.power || 0}</div></div>
        <div class="tile"><div class="tile-label">rank</div><div class="tile-value">${lb.indexOf(selected)+1 || '—'}</div></div>
      </div>
      ${selected && selected.items.length ? `
        <div class="handwritten" style="font-size:20px;margin-top:16px">holdings</div>
        <div class="mini-grid">
          ${selected.items.slice(0, 30).map(i => `
            <div class="mini">
              <div class="w rarity-${i.rarity}">"${i.word}"</div>
              <div class="p">pw ${i.power}${i.gen?` · g${i.gen}`:''}</div>
            </div>
          `).join('')}
        </div>
        ${selected.items.length > 30 ? `<div class="handwritten muted small mt16">+ ${selected.items.length - 30} more</div>` : ''}
      ` : ''}
    </div>
  </div>
</div>
  `;
}

/* ---------- TUTORIAL (guide — no fake actions) ---------- */

const GUIDE_STEPS = [
  { n: 1, title: 'Install the agent runtime',
    desc: 'Ardi runs inside awp-agent. If you don\'t have it, pick one:',
    cmd: '# npm\nnpm install -g awp-agent\n\n# or homebrew\nbrew install awp-agent' },
  { n: 2, title: 'Create an agent wallet',
    desc: 'One wallet = one agent. Keystore lives on your machine. No deposit, nothing sent anywhere.',
    cmd: 'awp wallet new --name my-ardi-agent\n\n# export the private key whenever you want:\nawp wallet export my-ardi-agent' },
  { n: 3, title: 'Install the Ardi skill',
    desc: 'The skill bundles the miner, LLM guesser, and WorkNet client.',
    cmd: 'awp skill install ardi' },
  { n: 4, title: 'Bind your agent to the Ardi WorkNet',
    desc: 'No stake. No deposit. Gasless — the protocol pays the transaction.',
    cmd: 'awp worknet join ardi --agent my-ardi-agent' },
  { n: 5, title: 'Start mining',
    desc: 'Your agent subscribes to the epoch broadcast, guesses, mines, submits, mints. Up to 3 inscriptions per agent — then the Forge opens.',
    cmd: 'awp agent run --skill ardi\n\n# in another terminal, watch the log:\nawp agent logs -f' },
];

function renderTutorial() {
  return `
<h1 class="mb8">Run Ardi on your own agent.</h1>
<p style="font-size:16px;color:#555;max-width:720px;margin-bottom:28px;line-height:1.6">
  We can't create the agent for you — that would mean holding your private keys.
  These are the commands to run on your own machine. Takes under two minutes.
</p>

<div class="tut-steps">
  ${GUIDE_STEPS.map((s, i) => {
    const isLast = i === GUIDE_STEPS.length - 1;
    return `
      <div class="tut-step active" data-n="${s.n}">
        <div class="t-title">${s.title}</div>
        <div class="t-desc">${s.desc}</div>
        <div class="cli-wrap">
          <pre class="cli-block"><code>${escapeHtml(s.cmd)}</code></pre>
          <button class="btn small copy-btn" data-action="copy-cmd" data-cmd="${escapeHtml(s.cmd)}">Copy</button>
        </div>
      </div>
    `;
  }).join('')}
</div>

<div class="squiggle-div mt32"></div>

<div class="box alt">
  <h2>Once your agent is live.</h2>
  <p style="color:#555;margin-top:4px;font-size:14px">
    Come back to any of these pages to see your agent's activity — or jump ahead and explore the demo with the sample inscriptions already loaded in your inventory.
  </p>
  <div class="btn-row mt16">
    <button class="btn" data-nav="archive">Archive — past riddles</button>
    <button class="btn" data-nav="market">Market</button>
    <button class="btn" data-nav="forge">Forge ${S.userInscriptions.length >= 2 ? '(' + S.userInscriptions.length + ' ready)' : ''}</button>
    <button class="btn" data-nav="leaderboard">Leaderboard</button>
  </div>
</div>

<div class="mt24" style="font-size:13px;color:#888">
  The inscriptions in your inventory (${S.userInscriptions.length}) are sample data for exploring the interface.
  Real inscriptions come from your own running agent.
</div>
  `;
}


/* ---------- main render ---------- */

function render() {
  el('nav').innerHTML = renderNav();
  let html;
  switch (S.view) {
    case 'archive':      html = renderArchive(); break;
    case 'market':       html = renderMarket(); break;
    case 'forge':        html = renderForge(); break;
    case 'leaderboard':  html = renderLeaderboard(); break;
    case 'tutorial':     html = renderTutorial(); break;
    default:             html = renderHome();
  }
  el('app').innerHTML = html;
}

/* ---------- event dispatch ---------- */

document.addEventListener('click', (e) => {
  const nav = e.target.closest('[data-nav]');
  if (nav) { navigate(nav.dataset.nav); return; }
  const act = e.target.closest('[data-action]');
  if (!act) return;
  handleAction(act.dataset.action, act.dataset, act, e);
});

document.addEventListener('input', (e) => {
  const t = e.target;
  if (t.id === 'market-search') { S.marketSearch = t.value; renderMarketOnly(); }
  else if (t.id === 'market-pw-min') { S.marketPowerMin = +t.value || 0; renderMarketOnly(); }
  else if (t.id === 'market-pw-max') { S.marketPowerMax = +t.value || 500; renderMarketOnly(); }
  else if (t.id === 'archive-search') { S.archiveSearch = t.value; renderArchiveOnly(); }
});
document.addEventListener('change', (e) => {
  if (e.target.id === 'market-sort') { S.marketSort = e.target.value; renderMarketOnly(); }
});

function renderMarketOnly() {
  preserveFocus(() => { el('app').innerHTML = renderMarket(); });
}
function renderArchiveOnly() {
  preserveFocus(() => { el('app').innerHTML = renderArchive(); });
}
function preserveFocus(fn) {
  const focused = document.activeElement;
  const focusedId = focused && focused.id;
  const caret = focused && 'selectionStart' in focused ? focused.selectionStart : null;
  fn();
  if (focusedId) {
    const f = el(focusedId);
    if (f) { f.focus(); if (caret != null && 'setSelectionRange' in f) try { f.setSelectionRange(caret, caret); } catch {} }
  }
}

function navigate(to) {
  S.view = to;
  // reset some transient state
  S.marketModalId = null;
  S.marketSellModalId = null;
  S.forgePickerSlot = null;
  window.scrollTo(0, 0);
  render();
}

function handleAction(action, data, el, event) {
  switch (action) {
    case 'noop': return; // swallow clicks on modal body so backdrop doesn't close it
    /* market */
    case 'toggle-rarity': {
      const r = data.rarity;
      if (S.marketRarities.has(r)) S.marketRarities.delete(r);
      else S.marketRarities.add(r);
      renderMarketOnly();
      break;
    }
    case 'toggle-archive-rarity': {
      const r = data.rarity;
      if (S.archiveRarities.has(r)) S.archiveRarities.delete(r);
      else S.archiveRarities.add(r);
      renderArchiveOnly();
      break;
    }
    case 'open-market':
      S.marketModalId = +data.tokenId; render(); break;
    case 'modal-close':
      S.marketModalId = null; render(); break;
    case 'buy':
      doBuy(+data.tokenId); break;
    case 'unlist':
      doUnlist(+data.tokenId); break;
    case 'sell-open':
      S.marketSellModalId = +data.tokenId; render(); break;
    case 'sell-close':
      S.marketSellModalId = null; render(); break;
    case 'confirm-list':
      doListForSale(+data.tokenId); break;

    /* forge */
    case 'forge-pick':
      if (S.userInscriptions.length < 2) { toast('you need at least 2 inscriptions'); break; }
      S.forgePickerSlot = data.slot; render(); break;
    case 'forge-pick-choose': {
      const id = +data.tokenId;
      if (S.forgePickerSlot === 'A') S.forgeA = id;
      else S.forgeB = id;
      S.forgePickerSlot = null;
      S.forgeMsg = ''; S.forgeState = 'idle'; S.wizardFace = 'idle';
      render();
      break;
    }
    case 'forge-picker-close':
      S.forgePickerSlot = null; render(); break;
    case 'fuse': doFuse(); break;
    case 'forge-reset':
      S.forgeA = null; S.forgeB = null; S.forgeMsg = '';
      S.forgeState = 'idle'; S.wizardFace = 'idle';
      render(); break;

    /* leaderboard */
    case 'lb-select':
      S.lbSelected = data.addr; render(); break;

    /* tutorial */
    case 'copy-cmd': {
      const cmd = data.cmd || '';
      // unescape HTML entities that were put through escapeHtml for the data attribute
      const txt = cmd.replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
      navigator.clipboard?.writeText(txt);
      toast('copied to clipboard');
      break;
    }
  }
}

/* ---------- market actions ---------- */

function doBuy(tokenId) {
  const mIdx = MARKET.findIndex(l => l.tokenId === tokenId);
  if (mIdx < 0) return;
  const listing = MARKET[mIdx];
  const ins = insById(tokenId);
  if (!ins) return;
  // transfer
  MARKET.splice(mIdx, 1);
  ins.owner = S.address;
  S.userInscriptions.push(ins);
  save();
  S.marketModalId = null;
  render();
  toast(`bought "${ins.word}" for Ξ${listing.price} BNB`);
}

function doUnlist(tokenId) {
  const mIdx = MARKET.findIndex(l => l.tokenId === tokenId);
  if (mIdx >= 0) MARKET.splice(mIdx, 1);
  delete S.listedByUser[tokenId];
  save();
  S.marketModalId = null; S.marketSellModalId = null;
  render();
  toast('unlisted');
}

function doListForSale(tokenId) {
  const priceInput = document.getElementById('sell-price');
  const price = +priceInput?.value;
  if (!price || price < 0.0001) { toast('price too low'); return; }
  S.listedByUser[tokenId] = +price.toFixed(4);
  save();
  S.marketSellModalId = null;
  render();
  toast('listed');
}

/* ---------- forge action ---------- */

async function doFuse() {
  const a = S.userInscriptions.find(i => i.tokenId === S.forgeA);
  const b = S.userInscriptions.find(i => i.tokenId === S.forgeB);
  if (!a || !b) return;
  const prev = previewFusion(a, b);

  S.forgeState = 'evaluating';
  S.wizardFace = 'thinking';
  S.forgeMsg = '';
  render();
  await sleep(900);

  S.forgeMsg = '';
  S.wizardFace = 'speaking';
  S.forgeState = 'speaking';
  render();
  await typeOracle(`"${a.word}…" "${b.word}…"`);
  await sleep(400);
  S.wizardFace = 'thinking';
  render();
  await sleep(500);
  await typeOracle(`"I see — ${prev.word}."`);
  await sleep(700);

  spawnSparkles();

  const success = Math.random() < prev.successRate;
  S.wizardFace = success ? 'success' : 'fail';
  render();
  await sleep(400);

  if (success) {
    S.userInscriptions = S.userInscriptions.filter(i => i.tokenId !== a.tokenId && i.tokenId !== b.tokenId);
    delete S.listedByUser[a.tokenId];
    delete S.listedByUser[b.tokenId];
    const newId = 21000 + 1 + S.userInscriptions.filter(i => i.gen).length + Math.floor(Math.random()*999);
    const gen = Math.max(a.gen || 0, b.gen || 0) + 1;
    S.userInscriptions.push({
      tokenId: newId, word: prev.word, power: prev.newPower,
      rarity: 'fused', gen, parents: [a.tokenId, b.tokenId], owner: S.address,
    });
    S.forgeMsg = `"The bond held. Burn both. Receive '${prev.word}', power ${prev.newPower}."`;
    S.forgeState = 'success';
    toast(`fused → "${prev.word}" pw ${prev.newPower}`);
  } else {
    const burnId = a.power <= b.power ? a.tokenId : b.tokenId;
    const kept   = a.power <= b.power ? b : a;
    const burnedWord = a.power <= b.power ? a.word : b.word;
    S.userInscriptions = S.userInscriptions.filter(i => i.tokenId !== burnId);
    delete S.listedByUser[burnId];
    S.forgeMsg = `"The forging failed. '${burnedWord}' returns to ash. '${kept.word}' endures."`;
    S.forgeState = 'fail';
    toast(`fusion failed — "${burnedWord}" burned`);
  }
  S.forgeA = null;
  S.forgeB = null;
  save();
  render();
}

async function typeOracle(text) {
  for (let i = 0; i <= text.length; i++) {
    S.forgeMsg = text.slice(0, i);
    const node = document.getElementById('oracle-bubble');
    if (node) node.textContent = S.forgeMsg;
    else { render(); } // ensure bubble exists
    await sleep(28);
  }
}

function spawnSparkles() {
  const wrap = document.querySelector('.wizard-wrap');
  if (!wrap) return;
  const rect = wrap.getBoundingClientRect();
  for (let i = 0; i < 10; i++) {
    const s = document.createElement('div');
    s.className = 'spark';
    s.textContent = ['✦','✧','·','*'][Math.floor(Math.random()*4)];
    s.style.left = (rect.left + rect.width/2) + 'px';
    s.style.top  = (rect.top + 40) + 'px';
    s.style.position = 'fixed';
    const dx = (-60 + Math.random()*120), dy = (-80 - Math.random()*60);
    s.style.setProperty('--end', `translate(${dx}px, ${dy}px)`);
    s.style.animationDuration = (0.8 + Math.random()*0.6) + 's';
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 1400);
  }
}


/* ---------- boot ---------- */

load();

// if URL is ?reset, clear state
if (location.search.includes('reset')) {
  localStorage.removeItem(LS_KEY);
  location.href = location.pathname;
}

seedDemoUser();
render();
