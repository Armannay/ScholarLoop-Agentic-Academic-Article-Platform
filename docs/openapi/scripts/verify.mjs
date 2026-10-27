#!/usr/bin/env node
/**
 * Verification gate for the OpenAPI spec.
 *
 * `redocly lint` proves the document is structurally valid OpenAPI 3.1. This script
 * proves things a linter cannot know about this API:
 *
 *   1. every operation has at least one request/response example (and a 4xx response,
 *      except /api/health and /api/version, which answer before auth or config exist);
 *   2. every example value validates against the schema it is attached to - including
 *      the CONTRACTS v1 schemas `$ref`-ed straight from docs/schemas/, so an example
 *      cannot drift from the frozen Pydantic models;
 *   3. every file under examples/ is referenced by the spec (no orphan fixtures), and
 *      every referenced file exists and is an Example Object;
 *   4. every SSE example parses into real frames whose `data:` payload validates against
 *      RunEvent;
 *   5. `ArticleSection` stays field-identical to the `SectionState` contract plus `text`;
 *   6. every operation declares `x-scholarloop-status` (implemented | planned);
 *   7. each Prism scenario, once merged with the spec, is still valid and still
 *      example-complete - a broken scenario fails here, not in the browser.
 *
 * Exit code 0 = everything checked. Any problem prints and exits 1.
 */

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'
import YAML from 'yaml'

const HERE = dirname(fileURLToPath(import.meta.url))
const SPEC_DIR = resolve(HERE, '..')
const REPO_ROOT = resolve(SPEC_DIR, '..', '..')
const SPEC_PATH = resolve(SPEC_DIR, 'openapi.yaml')
const SCHEMAS_DIR = resolve(REPO_ROOT, 'docs', 'schemas')
const EXAMPLES_DIR = resolve(SPEC_DIR, 'examples')
const SCENARIO_DIR = resolve(SPEC_DIR, 'mock', 'scenarios')

const SPEC_ID = 'urn:scholarloop:ui-api:v1'
const SCHEMA_NS = 'https://scholarloop.local/schemas/v1'
const PUBLIC_OPERATIONS = new Set(['getHealth', 'getVersion'])

const problems = []
const notes = []
const fail = (message) => problems.push(message)

// --------------------------------------------------------------------------- ajv
const ajv = new Ajv2020({ strict: false, allErrors: true, allowUnionTypes: true, verbose: false })
addFormats(ajv)

const contractSchemas = new Map()
for (const file of readdirSync(SCHEMAS_DIR).filter((name) => name.endsWith('.json'))) {
  const doc = JSON.parse(readFileSync(resolve(SCHEMAS_DIR, file), 'utf8'))
  contractSchemas.set(file, doc)
  try {
    ajv.addSchema(doc)
  } catch (error) {
    fail(`could not register contract schema ${file}: ${error.message}`)
  }
}

/** Point every `../schemas/<Name>.json` ref at the contract's registered `$id`. */
function rewriteExternalRefs(node) {
  if (Array.isArray(node)) return node.map(rewriteExternalRefs)
  if (node === null || typeof node !== 'object') return node
  const out = {}
  for (const [key, value] of Object.entries(node)) {
    if (key === '$ref' && typeof value === 'string' && value.startsWith('../schemas/')) {
      out[key] = value.replace('../schemas/', `${SCHEMA_NS}/`)
    } else {
      out[key] = rewriteExternalRefs(value)
    }
  }
  return out
}

/** Make internal `#/...` refs absolute so a copy can be registered under its own id. */
function absolutize(node, specId) {
  if (Array.isArray(node)) return node.map((item) => absolutize(item, specId))
  if (node === null || typeof node !== 'object') return node
  const out = {}
  for (const [key, value] of Object.entries(node)) {
    if (key === '$ref' && typeof value === 'string' && value.startsWith('#/')) {
      out[key] = `${specId}#${value.slice(1)}`
    } else {
      out[key] = absolutize(value, specId)
    }
  }
  return out
}

function loadSpec() {
  const raw = YAML.parse(readFileSync(SPEC_PATH, 'utf8'))
  return rewriteExternalRefs(raw)
}

//: Which document the pointers below are compiled against (the spec, or a scenario).
let activeSpecId = SPEC_ID

/**
 * Compile one schema of the active document.
 *
 * The whole document is registered as a schema, so `#/components/schemas/X` pointers
 * resolve exactly as they do for a client.
 */
function compile(pointer) {
  try {
    return ajv.compile({ $ref: `${activeSpecId}#${pointer}` })
  } catch (error) {
    fail(`schema ${pointer} does not compile: ${error.message}`)
    return null
  }
}

// --------------------------------------------------------------------------- helpers
function loadExampleObject(refPath) {
  const absolute = resolve(SPEC_DIR, refPath.replace(/^\.\//, ''))
  if (!existsSync(absolute)) {
    fail(`example file is missing: ${refPath}`)
    return null
  }
  const text = readFileSync(absolute, 'utf8')
  let parsed
  try {
    parsed = refPath.endsWith('.json') ? JSON.parse(text) : YAML.parse(text)
  } catch (error) {
    fail(`example file is not parseable (${refPath}): ${error.message}`)
    return null
  }
  if (parsed === null || typeof parsed !== 'object' || !('value' in parsed)) {
    fail(`example file is not an Example Object (needs a 'value'): ${refPath}`)
    return null
  }
  return parsed
}

function eachOperation(spec) {
  const operations = []
  for (const [pathKey, pathItem] of Object.entries(spec.paths ?? {})) {
    for (const [method, operation] of Object.entries(pathItem)) {
      if (['parameters', 'summary', 'description', 'servers', '$ref'].includes(method)) continue
      operations.push({ pathKey, method, operation })
    }
  }
  return operations
}

const referencedExamples = new Set()

/**
 * Validate every example of one media-type map.
 *
 * Returns the number of examples checked.
 */
function checkExamples({ mediaType, media, schemaPointer, where, seenRunEvent }) {
  let checked = 0
  const examples = media.examples ?? {}
  if (Object.keys(examples).length === 0 && !media.schema) {
    fail(`${where}: no schema and no examples`)
  }
  if (Object.keys(examples).length === 0) {
    fail(`${where}: no example (every response and request body must carry one)`)
    return 0
  }

  for (const [name, example] of Object.entries(examples)) {
    let resolved = example
    if (example && typeof example.$ref === 'string') {
      referencedExamples.add(example.$ref.replace(/^\.\//, ''))
      resolved = loadExampleObject(example.$ref)
      if (!resolved) continue
    }
    if (!resolved || typeof resolved !== 'object' || !('value' in resolved)) {
      fail(`${where} [${name}]: not an Example Object with a 'value'`)
      continue
    }
    const value = resolved.value
    checked += 1

    if (mediaType === 'text/event-stream') {
      if (typeof value !== 'string') {
        fail(`${where} [${name}]: an SSE example must be a string of frames`)
        continue
      }
      const frames = value.trimEnd().split(/\n\n/)
      for (const frame of frames) {
        const lines = frame.split('\n')
        const dataLine = lines.find((line) => line.startsWith('data: '))
        const idLine = lines.find((line) => line.startsWith('id: '))
        const eventLine = lines.find((line) => line.startsWith('event: '))
        if (!dataLine || !idLine || !eventLine) {
          fail(`${where} [${name}]: every frame needs id:, event: and data: lines`)
          continue
        }
        let payload
        try {
          payload = JSON.parse(dataLine.slice('data: '.length))
        } catch (error) {
          fail(`${where} [${name}]: data: is not JSON (${error.message})`)
          continue
        }
        if (payload.seq !== Number(idLine.slice('id: '.length))) {
          fail(`${where} [${name}]: the id: line must equal payload.seq`)
        }
        if (payload.type !== eventLine.slice('event: '.length)) {
          fail(`${where} [${name}]: the event: line must equal payload.type`)
        }
        seenRunEvent.count += 1
      }
      continue
    }

    if (!schemaPointer) continue
    const validate = compile(schemaPointer)
    if (validate && !validate(value)) {
      const detail = ajv.errorsText(validate.errors, { separator: '; ' })
      fail(`${where} [${name}] does not validate against ${schemaPointer}: ${detail}`)
    }
  }
  return checked
}

function mediaTypeToPointer(media) {
  const schema = media?.schema
  if (!schema) return null
  if (typeof schema.$ref === 'string' && schema.$ref.startsWith(`#/components/schemas/`)) {
    return schema.$ref.slice(1)
  }
  return null
}

// --------------------------------------------------------------------------- checks
function register(spec, specId) {
  try {
    ajv.addSchema({ ...spec, $id: specId })
    return true
  } catch (error) {
    fail(`the document cannot be registered as a JSON Schema (${specId}): ${error.message}`)
    return false
  }
}

function checkSpec(spec, { label, requireStatus, specId = SPEC_ID }) {
  const seenRunEvent = { count: 0 }
  let exampleCount = 0
  let operationCount = 0
  activeSpecId = specId

  for (const { pathKey, method, operation } of eachOperation(spec)) {
    operationCount += 1
    const where = `${method.toUpperCase()} ${pathKey}`
    const operationId = operation.operationId
    if (!operationId) fail(`${label} ${where}: missing operationId`)
    if (!operation.summary) fail(`${label} ${where}: missing summary`)
    if (!operation.description) fail(`${label} ${where}: missing description`)
    if (!Array.isArray(operation.tags) || operation.tags.length === 0) {
      fail(`${label} ${where}: missing tags`)
    }
    if (requireStatus && !['implemented', 'planned'].includes(operation['x-scholarloop-status'])) {
      fail(`${label} ${where}: x-scholarloop-status must be 'implemented' or 'planned'`)
    }

    const responses = operation.responses ?? {}
    const codes = Object.keys(responses)
    const hasClientError = codes.some((code) => /^4/.test(code))
    if (!hasClientError && !PUBLIC_OPERATIONS.has(operationId)) {
      fail(`${label} ${where}: no 4xx response (only /api/health and /api/version may omit one)`)
    }
    if (!codes.some((code) => /^[23]/.test(code))) {
      fail(`${label} ${where}: no success response`)
    }

    if (operation.requestBody?.content) {
      for (const [mediaType, media] of Object.entries(operation.requestBody.content)) {
        exampleCount += checkExamples({
          mediaType,
          media,
          schemaPointer: mediaTypeToPointer(media),
          where: `${label} ${where} requestBody ${mediaType}`,
          seenRunEvent,
        })
      }
    }

    for (const [code, response] of Object.entries(responses)) {
      if (response.$ref) {
        const name = response.$ref.split('/').pop()
        const shared = spec.components?.responses?.[name]
        if (!shared) {
          fail(`${label} ${where} ${code}: unresolved shared response ${response.$ref}`)
          continue
        }
        for (const [mediaType, media] of Object.entries(shared.content ?? {})) {
          exampleCount += checkExamples({
            mediaType,
            media,
            schemaPointer: mediaTypeToPointer(media),
            where: `${label} ${where} ${code} (${name})`,
            seenRunEvent,
          })
        }
        continue
      }
      for (const [mediaType, media] of Object.entries(response.content ?? {})) {
        exampleCount += checkExamples({
          mediaType,
          media,
          schemaPointer: mediaTypeToPointer(media),
          where: `${label} ${where} ${code}`,
          seenRunEvent,
        })
      }
    }
  }

  if (seenRunEvent.count === 0) fail(`${label}: no SSE frame validated against RunEvent`)

  // Every file under examples/ must be referenced: an orphan is a fixture nobody reads.
  // Scenario overlays reference their own payloads, so they count as references too.
  const walk = (directory) =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
      entry.isDirectory() ? walk(resolve(directory, entry.name)) : [resolve(directory, entry.name)],
    )
  if (label === 'spec') {
    for (const file of walk(EXAMPLES_DIR)) {
      const rel = relative(SPEC_DIR, file).split('\\').join('/')
      if (!referencedExamples.has(rel) && !scenarioReferenced.has(rel)) {
        fail(`${label}: ${rel} is not referenced by the spec or by any mock scenario`)
      }
    }
  }

  notes.push(
    `${label}: ${operationCount} operations, ${exampleCount} examples validated, ` +
      `${seenRunEvent.count} SSE frames` +
      (label === 'spec' ? `, ${referencedExamples.size} example files referenced` : ''),
  )
}

function checkArticleSectionParity(spec) {
  const article = spec.components?.schemas?.ArticleSection?.properties
  const state = contractSchemas.get('SectionState.json')
  if (!article || !state) {
    fail('ArticleSection parity check: missing ArticleSection or docs/schemas/SectionState.json')
    return
  }
  const articleFields = new Set(Object.keys(article))
  articleFields.delete('text')
  const stateFields = new Set(Object.keys(state.properties ?? {}))
  const missing = [...stateFields].filter((field) => !articleFields.has(field))
  const extra = [...articleFields].filter((field) => !stateFields.has(field))
  if (missing.length > 0) fail(`ArticleSection is missing SectionState fields: ${missing.join(', ')}`)
  if (extra.length > 0) fail(`ArticleSection has fields SectionState does not: ${extra.join(', ')}`)
  notes.push('ArticleSection = SectionState + text (field-for-field)')
}

// --------------------------------------------------------------------------- merge (scenarios)
const isPlainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

function mergeOverlay(base, overlay) {
  if (!isPlainObject(base) || !isPlainObject(overlay)) return overlay
  const merged = { ...base }
  for (const [key, value] of Object.entries(overlay)) {
    if (key === 'examples' && isPlainObject(value) && isPlainObject(merged.examples)) {
      const ordered = {}
      for (const [name, example] of Object.entries(value)) {
        ordered[name] = isPlainObject(merged.examples[name])
          ? mergeOverlay(merged.examples[name], example)
          : example
      }
      for (const [name, example] of Object.entries(merged.examples)) {
        if (!(name in ordered)) ordered[name] = example
      }
      merged.examples = ordered
    } else if (isPlainObject(value) && isPlainObject(merged[key])) {
      merged[key] = mergeOverlay(merged[key], value)
    } else {
      merged[key] = value
    }
  }
  return merged
}

// --------------------------------------------------------------------------- run
// Scenario overlays reference scenario payloads; collect them before the orphan check.
const scenarioReferenced = new Set()
function collectRefs(node) {
  if (Array.isArray(node)) {
    for (const item of node) collectRefs(item)
    return
  }
  if (node === null || typeof node !== 'object') return
  for (const [key, value] of Object.entries(node)) {
    if (key === '$ref' && typeof value === 'string' && value.includes('examples/')) {
      scenarioReferenced.add(value.replace(/^\.\//, ''))
    } else {
      collectRefs(value)
    }
  }
}
if (existsSync(SCENARIO_DIR)) {
  for (const file of readdirSync(SCENARIO_DIR).filter((name) => name.endsWith('.yaml'))) {
    collectRefs(YAML.parse(readFileSync(resolve(SCENARIO_DIR, file), 'utf8')))
  }
}

const spec = loadSpec()
if (register(spec, SPEC_ID)) {
  checkSpec(spec, { label: 'spec', requireStatus: true })
}
checkArticleSectionParity(spec)

const scenarioNames = []
if (existsSync(SCENARIO_DIR)) {
  for (const file of readdirSync(SCENARIO_DIR).filter((name) => name.endsWith('.yaml'))) {
    scenarioNames.push(file.replace(/\.yaml$/, ''))
  }
}
if (scenarioNames.length !== 3) {
  fail(`expected three Prism scenarios, found ${scenarioNames.length}: ${scenarioNames.join(', ')}`)
}
for (const name of scenarioNames) {
  const overlay = YAML.parse(readFileSync(resolve(SCENARIO_DIR, `${name}.yaml`), 'utf8'))
  const meta = overlay['x-scholarloop-scenario']
  if (!meta?.id || !meta?.title || !Array.isArray(meta?.story)) {
    fail(`scenario ${name}: x-scholarloop-scenario needs id, title and story`)
  }
  if (meta?.id && meta.id !== name) {
    fail(`scenario ${name}: x-scholarloop-scenario.id is '${meta.id}'`)
  }
  const scenarioId = `urn:scholarloop:ui-api:v1:${name}`
  const merged = absolutize(
    rewriteExternalRefs(mergeOverlay(loadSpec(), overlay)),
    scenarioId,
  )
  if (!register(merged, scenarioId)) continue
  checkSpec(merged, { label: `scenario:${name}`, requireStatus: false, specId: scenarioId })
  // Scenario documents share component names with the base spec; drop before the next one.
  ajv.removeSchema(scenarioId)
}

// --------------------------------------------------------------------------- report
if (problems.length > 0) {
  console.error(`\n✖ ${problems.length} problem(s) in the OpenAPI spec:\n`)
  for (const problem of problems) console.error(`  - ${problem}`)
  console.error('')
  process.exit(1)
}

console.log('\n✔ OpenAPI spec verified\n')
for (const note of notes) console.log(`  ${note}`)
console.log('')
