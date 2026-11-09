// Uses the explicitly supplied official meshoptimizer module (MIT); no networking.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [modulePath, countText, strideText, inputPath, outputPath] = process.argv.slice(2);
try {
  const count = Number(countText), stride = Number(strideText);
  if (!path.isAbsolute(modulePath ?? '') || !Number.isSafeInteger(count) || count < 0 ||
      stride !== 36 || count * stride > 128 * 1024 * 1024)
    throw new Error('invalid_arguments');
  if (fs.statSync(inputPath).size > 128 * 1024 * 1024) throw new Error('input_limit');
  const input = fs.readFileSync(inputPath);
  const module = await import(pathToFileURL(modulePath).href);
  const decoder = module.MeshoptDecoder ?? module.default;
  if (!decoder || decoder.supported === false) throw new Error('decoder_unavailable');
  await decoder.ready;
  const output = new Uint8Array(count * stride);
  decoder.decodeVertexBuffer(output, count, stride, input);
  const bytes = Buffer.from(output.buffer, output.byteOffset, output.byteLength);
  if (outputPath) {
    fs.writeFileSync(outputPath + '.tmp', bytes);
    fs.renameSync(outputPath + '.tmp', outputPath);
  } else {
    process.stdout.write(bytes);
  }
} catch (error) {
  process.stderr.write('meshopt_bridge_failed\n');
  process.exitCode = 1;
}
