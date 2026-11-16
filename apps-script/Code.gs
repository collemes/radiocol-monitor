/**
 * RADIOCOL — fila de aprovação das solicitações das emissoras
 * ------------------------------------------------------------
 * 1. O formulário do site envia para este script (Web App).
 * 2. Cada solicitação vira uma linha na planilha (privada, só você vê),
 *    já com o post encontrado e o stream que está hoje no site, para comparar.
 * 3. Você escolhe "Aprovar" ou "Recusar" na coluna Decisão.
 *    - Aprovar (atualização/remoção): envia para o GitHub, que grava no radios.json
 *      e o site passa a usar os dados novos em poucos minutos.
 *    - Aprovar (novo cadastro): marca para você criar o post no Blogger.
 *    - Recusar: avisa o solicitante com o motivo (opcional).
 * Dados pessoais do solicitante (nome, e-mail, telefone) NUNCA vão para o GitHub.
 *
 * Instalação: veja README (seção "Atualização pelas próprias emissoras").
 */

var CONFIG = {
  GITHUB_REPO: 'SEU-USUARIO/radiocol-monitor',
  PAGES_BASE: 'https://SEU-USUARIO.github.io/radiocol-monitor/',
  ADMIN_EMAIL: 'redecolbrazil@gmail.com',
  SITE: 'https://www.radiocol.com.br',
  SHARDS: 32,
  EMAIL_SOLICITANTE: true,          // avisar quem pediu quando aprovar/recusar
  SHEET_NAME: 'Solicitações',
  MAX_POR_HORA_POR_EMAIL: 5
};

// Colunas da planilha (a ordem define a posição)
var COLS = [
  ['recebido', 'Recebido em'], ['protocolo', 'Protocolo'], ['tipo', 'Tipo'], ['radio', 'Rádio'],
  ['pagina', 'Página atual da rádio'], ['post_id', 'Post ID'],
  ['stream', 'Stream solicitado (editável)'], ['stream_atual', 'Stream atual no site'],
  ['situacao_atual', 'Situação do stream atual'],
  ['whatsapp', 'WhatsApp (editável)'], ['telefone', 'Telefone (editável)'], ['outros', 'Outros dados'],
  ['nome', 'Solicitante'], ['relacao', 'Relação com a rádio'], ['email', 'E-mail'],
  ['tel_solicitante', 'Tel./WhatsApp do solicitante'], ['obs', 'Observações'],
  ['decisao', 'Decisão'], ['motivo', 'Motivo (se recusar)'], ['situacao', 'Situação'], ['bruto', 'Dados brutos']
];
var C = {};
COLS.forEach(function (c, i) { C[c[0]] = i + 1; });

// Campos do formulário do Radiocol (name="...") -> coluna da planilha
var CAMPOS = {
  tipo: 'tipo', pagina_atual: 'pagina', nome: 'radio', link_audio: 'stream', link_video: 'video',
  whatsapp: 'whatsapp', telefone: 'telefone', responsavel: 'nome', vinculo: 'relacao', email: 'email',
  telefone_solicitante: 'tel_solicitante', observacoes: 'obs', site_url_extra: 'honeypot'
};
// Rótulos dos demais campos (vão para "Outros dados")
var ROTULOS = {
  emissora: 'Tipo de emissora', frequencia: 'Frequência', genero: 'Gênero', cidade: 'Cidade', uf: 'UF',
  link_logo: 'Logotipo', descricao: 'Sobre a rádio', site: 'Site', redes_sociais: 'Redes sociais',
  comercial: 'Comercial', endereco: 'Endereço', origem: 'Enviado de'
};

// Para campos com outros nomes: reconhece pelo nome (name="...") — sem acento, minúsculo.
// A primeira regra que casar vence. Ajuste se algum campo não for reconhecido.
var REGRAS = [
  ['honeypot', /(nao_preencha|honeypot|^hp$|_hp$|website2)/],
  ['tipo', /(tipo_de_solicitacao|solicitacao|^acao$|^tipo_pedido|^pedido)/],
  ['pagina', /(pagina|url_post|link_post|post_url|link_atual)/],
  ['stream', /(stream|audio|transmissao_em_audio|link_transmissao)/],
  ['video', /(video|m3u8_video)/],
  ['tel_solicitante', /(seu_telefone|telefone_solicitante|seu_whats|contato_solicitante)/],
  ['whatsapp', /whats|zap/],
  ['telefone', /(telefone|fone|celular)/],
  ['nome', /(seu_nome|nome_solicitante|solicitante)/],
  ['relacao', /relacao/],
  ['email', /e?_?mail/],
  ['radio', /(nome_da_radio|nome_radio|^radio$|nome_da_emissora|^nome$)/],
  ['obs', /(observa|mensagem|comentario)/],
  ['termo', /(termo|aceite|consent|concordo)/]
];

function norm_(s) {
  return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

function mapear_(params) {
  var out = { outros: {} };
  Object.keys(params).forEach(function (k) {
    var v = String(params[k] || '').trim();
    if (!v) return;
    var n = norm_(k), campo = CAMPOS[k] || null;
    if (!campo && !ROTULOS[k]) {
      for (var i = 0; i < REGRAS.length; i++) if (REGRAS[i][1].test(n)) { campo = REGRAS[i][0]; break; }
    }
    if (campo && !out[campo]) out[campo] = v;
    else out.outros[ROTULOS[k] || k] = v;
  });
  return out;
}

function tipoNormal_(t) {
  var n = norm_(t);
  if (/cadastr|nova/.test(n)) return 'Cadastrar nova rádio';
  if (/remo[vc]|exclu|corre/.test(n)) return 'Remover / corrigir';
  return 'Atualizar dados';
}

// ---------------------------------------------------------------- recebimento
function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var params = (e && e.parameter) || {};
    if (e && e.postData && /^\s*\{/.test(e.postData.contents || '')) {
      try { params = JSON.parse(e.postData.contents); } catch (x) {}
    }
    var d = mapear_(params);
    if (d.honeypot) return json_({ ok: true });                       // robô de spam: ignora em silêncio
    if (!d.radio && !d.pagina) return json_({ ok: false, erro: 'Informe a rádio.' });
    if (d.email && !limite_(d.email)) return json_({ ok: false, erro: 'Muitas solicitações. Tente mais tarde.' });

    d.tipo = tipoNormal_(d.tipo || (d.pagina ? 'atualizar' : 'cadastrar'));
    d.protocolo = 'R-' + Utilities.formatDate(new Date(), 'America/Sao_Paulo', 'yyyyMMdd') + '-' +
                  Math.floor(1000 + Math.random() * 9000);
    d.recebido = new Date();
    if (d.video) d.outros['Vídeo'] = d.video;

    // Localiza o post e o que está hoje no site
    if (d.pagina) {
      var info = situacaoAtual_(d.pagina);
      d.post_id = info.id || 'não encontrado';
      d.stream_atual = info.u || '';
      d.situacao_atual = info.situacao || '';
    }

    var sh = planilha_();
    var linha = COLS.map(function (c) {
      var k = c[0];
      if (k === 'outros') return Object.keys(d.outros).map(function (x) { return x + ': ' + d.outros[x]; }).join('\n');
      if (k === 'decisao') return 'Pendente';
      if (k === 'bruto') return JSON.stringify(params);
      return d[k] || '';
    });
    sh.appendRow(linha);

    MailApp.sendEmail({
      to: CONFIG.ADMIN_EMAIL,
      subject: 'Radiocol: ' + d.tipo + ' — ' + (d.radio || d.pagina) + ' (' + d.protocolo + ')',
      htmlBody: '<p><b>' + esc_(d.tipo) + '</b> — ' + esc_(d.radio || '') + '</p>' +
        '<p>Página: ' + esc_(d.pagina || '—') + '<br>Stream pedido: ' + esc_(d.stream || '—') +
        '<br>Stream atual: ' + esc_(d.stream_atual || '—') + ' (' + esc_(d.situacao_atual || '') + ')' +
        '<br>WhatsApp: ' + esc_(d.whatsapp || '—') + ' · Telefone: ' + esc_(d.telefone || '—') + '</p>' +
        '<p>Solicitante: ' + esc_(d.nome || '') + ' (' + esc_(d.relacao || '') + ') — ' + esc_(d.email || '') + '</p>' +
        '<p><a href="' + SpreadsheetApp.getActiveSpreadsheet().getUrl() + '">Abrir a planilha para aprovar</a></p>'
    });
    return json_({ ok: true, protocolo: d.protocolo });
  } finally {
    lock.releaseLock();
  }
}

function doGet() { return json_({ ok: true, servico: 'radiocol-solicitacoes' }); }

function situacaoAtual_(pagina) {
  var path = String(pagina).replace(/^https?:\/\/[^\/]+/i, '').split('?')[0].split('#')[0];
  try {
    var paths = JSON.parse(UrlFetchApp.fetch(CONFIG.PAGES_BASE + 'paths.json', { muteHttpExceptions: true }).getContentText());
    var id = paths[path];
    if (!id) return {};
    var mapa = JSON.parse(UrlFetchApp.fetch(CONFIG.PAGES_BASE + 'r/' + shard_(id) + '.json',
                                            { muteHttpExceptions: true }).getContentText());
    var e = mapa[id] || {};
    var sit = !e.u ? 'sem stream' : e.s === 0 ? 'FORA DO AR' : e.h ? 'ok (só http)' : 'ok';
    return { id: id, u: e.u, situacao: sit };
  } catch (err) {
    return {};
  }
}

function shard_(p) {               // igual ao robô e ao script do tema
  var h = 0;
  for (var i = 0; i < p.length; i++) h = (Math.imul(h, 31) + p.charCodeAt(i)) >>> 0;
  return ('0' + (h % CONFIG.SHARDS)).slice(-2);
}

function limite_(email) {
  var cache = CacheService.getScriptCache(), k = 'lim_' + norm_(email);
  var n = Number(cache.get(k) || 0);
  if (n >= CONFIG.MAX_POR_HORA_POR_EMAIL) return false;
  cache.put(k, String(n + 1), 3600);
  return true;
}

// ---------------------------------------------------------------- decisão
function aoEditar(e) {
  var sh = e.range.getSheet();
  if (sh.getName() !== CONFIG.SHEET_NAME || e.range.getColumn() !== C.decisao || e.range.getRow() < 2) return;
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    processarLinha_(sh, e.range.getRow());
  } finally {
    lock.releaseLock();
  }
}

function processarLinha_(sh, row) {
  var v = sh.getRange(row, 1, 1, COLS.length).getValues()[0];
  var get = function (k) { return String(v[C[k] - 1] || '').trim(); };
  var decisao = get('decisao'), situacao = get('situacao'), quando = agora_();
  if (/^(Enviado|Recusado|Aprovado)/.test(situacao)) return;  // já processada (apague a Situação para reenviar)

  if (decisao === 'Recusar') {
    setSit_(sh, row, 'Recusado em ' + quando);
    avisar_(get('email'), 'Sua solicitação ' + get('protocolo') + ' não foi aprovada',
      'Olá ' + get('nome') + ',\n\nAnalisamos sua solicitação para "' + get('radio') + '" e ela não foi aprovada.' +
      (get('motivo') ? '\nMotivo: ' + get('motivo') : '') + '\n\nEquipe Radiocol');
    return;
  }
  if (decisao !== 'Aprovar' && decisao !== 'Aprovar remoção') return;
  var remover = decisao === 'Aprovar remoção';

  var tipo = get('tipo');
  if (/Cadastrar/.test(tipo) && !remover) {
    setSit_(sh, row, 'Aprovado em ' + quando + ' — criar o post no Blogger');
    avisar_(get('email'), 'Sua rádio foi aprovada no Radiocol (' + get('protocolo') + ')',
      'Olá ' + get('nome') + ',\n\nO cadastro de "' + get('radio') + '" foi aprovado e será publicado em breve.\n\nEquipe Radiocol');
    return;
  }
  if (!get('post_id') || get('post_id') === 'não encontrado') {
    setSit_(sh, row, 'ERRO: post não encontrado — corrija a coluna "Página atual da rádio" e aprove de novo');
    sh.getRange(row, C.decisao).setValue('Pendente');
    return;
  }
  // "Aprovar" = atualizar/corrigir dados ; "Aprovar remoção" = parar de monitorar a rádio
  var payload = { id: get('protocolo'), tipo: remover ? 'remover' : 'atualizar', post: get('pagina'), post_id: get('post_id') };
  if (!remover) {
    if (get('stream') && get('stream') !== get('stream_atual')) payload.stream = get('stream');
    if (get('whatsapp')) payload.whatsapp = get('whatsapp');
    if (get('telefone')) payload.telefone = get('telefone');
    if (!payload.stream && !payload.whatsapp && !payload.telefone) {
      setSit_(sh, row, 'Nada para enviar ao site (stream igual ao atual e sem contatos) — outros dados: atualizar no post');
      return;
    }
  }
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) { setSit_(sh, row, 'ERRO: falta GITHUB_TOKEN nas propriedades do script'); return; }
  var resp = UrlFetchApp.fetch('https://api.github.com/repos/' + CONFIG.GITHUB_REPO + '/dispatches', {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28' },
    payload: JSON.stringify({ event_type: 'aplicar-solicitacao', client_payload: payload })
  });
  if (resp.getResponseCode() !== 204) {
    setSit_(sh, row, 'ERRO GitHub ' + resp.getResponseCode() + ': ' + resp.getContentText().slice(0, 200));
    sh.getRange(row, C.decisao).setValue('Pendente');
    return;
  }
  setSit_(sh, row, 'Enviado ao site em ' + quando + ' (confira em GitHub → Actions)');
  avisar_(get('email'), 'Atualização aprovada: ' + get('radio') + ' (' + get('protocolo') + ')',
    'Olá ' + get('nome') + ',\n\nSua solicitação foi aprovada. Em até 15 minutos as informações novas aparecem em:\n' +
    get('pagina') + '\n\nObrigado por manter sua emissora atualizada!\nEquipe Radiocol');
}

// ---------------------------------------------------------------- utilitários
function setSit_(sh, row, txt) { sh.getRange(row, C.situacao).setValue(txt); }
function agora_() { return Utilities.formatDate(new Date(), 'America/Sao_Paulo', 'dd/MM/yyyy HH:mm'); }
function esc_(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
function avisar_(email, assunto, texto) {
  if (!CONFIG.EMAIL_SOLICITANTE || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email || '')) return;
  MailApp.sendEmail({ to: email, subject: assunto, body: texto, replyTo: CONFIG.ADMIN_EMAIL, name: 'Radiocol' });
}

function planilha_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(CONFIG.SHEET_NAME) || configurar();
}

/** Rode UMA vez (menu Executar > configurar): cria a aba, o menu de decisão e o gatilho. */
function configurar() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(CONFIG.SHEET_NAME) || ss.insertSheet(CONFIG.SHEET_NAME);
  sh.getRange(1, 1, 1, COLS.length).setValues([COLS.map(function (c) { return c[1]; })])
    .setFontWeight('bold').setBackground('#f3f3f3');
  sh.setFrozenRows(1);
  var regra = SpreadsheetApp.newDataValidation().requireValueInList(['Pendente', 'Aprovar', 'Aprovar remoção', 'Recusar'], true).build();
  sh.getRange(2, C.decisao, 2000, 1).setDataValidation(regra);
  sh.getRange(2, C.decisao, 2000, 1).setBackground('#fff8e1');
  sh.setColumnWidth(C.stream, 280); sh.setColumnWidth(C.stream_atual, 280); sh.setColumnWidth(C.situacao, 300);
  sh.hideColumns(C.bruto);
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'aoEditar') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('aoEditar').forSpreadsheet(ss).onEdit().create();
  return sh;
}
