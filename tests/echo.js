// A stand-in for Claude Code in the terminal tests: a Node program in raw mode, as Claude Code is,
// that prints each key, paste and piece of text it receives on a line of its own.
process.stdin.setRawMode(true);
process.stdin.setEncoding("utf8");
process.stdout.write("\x1b[?2004h"); // Ask for bracketed paste, as Claude Code does.
process.stdout.write("READY\r\n");

const KEYS = { "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1bOA": "up", "\x1bOB": "down", "\r": "enter", "\x1b": "esc", "\t": "tab" };
const PASTE_START = "\x1b[200~", PASTE_END = "\x1b[201~";
let buffer = "";

function show(line) {
  process.stdout.write(line + "\r\n");
}

process.stdin.on("data", (data) => {
  buffer += data;
  while (buffer) {
    if (buffer.startsWith(PASTE_START)) {
      const end = buffer.indexOf(PASTE_END);
      if (end < 0) return; // The rest of the paste is still coming.
      show("PASTE[" + JSON.stringify(buffer.slice(PASTE_START.length, end)) + "]");
      buffer = buffer.slice(end + PASTE_END.length);
      continue;
    }
    const key = Object.keys(KEYS).sort((a, b) => b.length - a.length).find((k) => buffer.startsWith(k));
    if (key) {
      show("KEY " + KEYS[key]);
      buffer = buffer.slice(key.length);
      continue;
    }
    const match = buffer.match(/^[^\x1b\r\t]+/);
    const text = match ? match[0] : buffer[0];
    show("TEXT " + text);
    buffer = buffer.slice(text.length);
  }
});
