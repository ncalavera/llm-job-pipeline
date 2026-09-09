import { test } from "node:test";
import { execFileSync } from "node:child_process";

test("the browser entry point links every imported export", () => {
  execFileSync(process.execPath, ["--experimental-vm-modules", "--input-type=module", "-e", `
    import vm from 'node:vm';
    import fs from 'node:fs';
    import path from 'node:path';
    const modules = new Map();
    function load(file) {
      file = path.resolve(file);
      if (!modules.has(file)) modules.set(file, new vm.SourceTextModule(
        fs.readFileSync(file, 'utf8'), { identifier: file }
      ));
      return modules.get(file);
    }
    await load('public/app.js').link((name, parent) =>
      load(path.resolve(path.dirname(parent.identifier), name))
    );
  `], { cwd: new URL("../../", import.meta.url), stdio: "pipe" });
});
