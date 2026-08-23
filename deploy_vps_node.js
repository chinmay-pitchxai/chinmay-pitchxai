const { Client } = require('ssh2');

const conn = new Client();

const commands = [
  'cd /root/technopolis',
  'git pull origin fix/master-update',
  'docker compose build --no-cache',
  'docker compose up -d',
  'echo "Waiting for services to start..." && sleep 10',
  'curl -sf http://localhost:9090/health && echo "\\n[OK] Backend healthy" || echo "[WARN] Backend not yet healthy"',
  'docker compose ps',
];

conn.on('ready', () => {
  console.log('SSH connected to VPS');

  let output = '';
  let currentCmd = '';

  const runNext = (index) => {
    if (index >= commands.length) {
      console.log('\n=== Deployment Complete ===');
      conn.end();
      return;
    }

    currentCmd = commands[index];
    console.log(`\n>>> Running: ${currentCmd}`);

    conn.exec(currentCmd, (err, stream) => {
      if (err) {
        console.error(`Error executing: ${currentCmd}`, err.message);
        runNext(index + 1);
        return;
      }

      stream.on('close', (code) => {
        console.log(`[exit code: ${code}]`);
        runNext(index + 1);
      }).on('data', (data) => {
        process.stdout.write(data.toString());
      }).stderr.on('data', (data) => {
        process.stderr.write(data.toString());
      });
    });
  };

  runNext(0);
});

conn.on('error', (err) => {
  console.error('SSH connection error:', err.message);
  process.exit(1);
});

conn.connect({
  host: '187.127.177.149',
  port: 22,
  username: 'root',
  // Try key-based auth first, fall back to password
  tryKeyboard: true,
  readyTimeout: 15000,
});

// Handle keyboard-interactive authentication
conn.on('keyboard-interactive', (name, instructions, instructionsLang, prompts, finish) => {
  console.log('Keyboard-interactive auth requested');
  // If password prompt, provide it
  finish();
});
