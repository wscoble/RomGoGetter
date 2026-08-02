// reset-questarr-password.js
// Run this ON the host where Questarr runs, from inside the Questarr app
// directory (the one containing node_modules with better-sqlite3 + bcryptjs):
//
//   node reset-questarr-password.js <username> <newpassword>
//
// It updates the user's password_hash in the SQLite DB in place. Set
// SQLITE_DB_PATH if your Questarr uses a non-default db location:
//
//   SQLITE_DB_PATH=/path/to/sqlite.db node reset-questarr-password.js scott@scoble.com newpass
//
// After this, log in to Questarr's UI with the new password.

const bcrypt = require("bcryptjs");
const path = require("path");
const Database = require("better-sqlite3");

const username = process.argv[2];
const newPassword = process.argv[3];
if (!username || !newPassword) {
  console.error("usage: node reset-questarr-password.js <username> <newpassword>");
  process.exit(1);
}

const dbPath = process.env.SQLITE_DB_PATH || path.join(process.cwd(), "sqlite.db");
console.log(`using db: ${dbPath}`);

const db = new Database(dbPath);
const hash = bcrypt.hashSync(newPassword, 10);

const info = db
  .prepare("UPDATE users SET password_hash = ? WHERE username = ?")
  .run(hash, username);

if (info.changes === 0) {
  console.error(`no user found with username "${username}". users:`);
  console.error(db.prepare("SELECT id, username FROM users").all());
  process.exit(1);
}
console.log(`updated ${info.changes} row(s). you can now log in as "${username}".`);
db.close();