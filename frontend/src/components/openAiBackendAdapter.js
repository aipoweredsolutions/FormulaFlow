function encodeConversationItem(item) {
  if (item.kind === "input" && item.type === "message") {
    return {
      type: "message",
      role: item.role,
      content: item.content.map((part) => {
        if (part.type === "text") {
          return { type: "input_text", text: part.text };
        }
        if (part.type === "image") {
          return {
            type: "input_image",
            detail: part.detail,
            file_id: part.fileId,
            image_url: part.imageUrl,
          };
        }
        return {
          type: "input_file",
          file_id: part.fileId,
          file_data: part.fileData,
          file_url: part.fileUrl,
          filename: part.filename,
        };
      }),
    };
  }

  if (item.type === "function_call_output") {
    return {
      type: "function_call_output",
      call_id: item.callId,
      output: item.output,
    };
  }

  if (item.kind === "output" && item.type === "message") {
    return {
      type: "message",
      role: "assistant",
      content: item.content.map((part) =>
        part.type === "text"
          ? { type: "output_text", text: part.text }
          : { type: "refusal", refusal: part.refusal },
      ),
    };
  }

  if (item.kind === "output" && item.type === "function_call") {
    return {
      type: "function_call",
      call_id: item.callId,
      name: item.name,
      arguments: item.arguments,
    };
  }

  if (item.kind === "output" && item.type === "reasoning") {
    return {
      id: item.id,
      type: "reasoning",
      summary: item.summary.map((part) => ({
        type: "summary_text",
        text: part.text,
      })),
      content: item.content?.map((part) => ({
        type: "reasoning_text",
        text: part.text,
      })),
    };
  }

  throw new Error(`Unsupported AG Studio conversation item: ${item.type}`);
}

function isSchema(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUndefinedSentinel(schema) {
  return (
    isSchema(schema) &&
    Object.keys(schema).length === 1 &&
    isSchema(schema.not) &&
    Object.keys(schema.not).length === 0
  );
}

function makeNullable(schema) {
  if (typeof schema.type === "string") {
    return schema.type === "null"
      ? schema
      : { ...schema, type: [schema.type, "null"] };
  }
  if (Array.isArray(schema.type)) {
    return schema.type.includes("null")
      ? schema
      : { ...schema, type: [...schema.type, "null"] };
  }
  if (Array.isArray(schema.anyOf)) {
    return schema.anyOf.some(
      (branch) => isSchema(branch) && branch.type === "null",
    )
      ? schema
      : { ...schema, anyOf: [...schema.anyOf, { type: "null" }] };
  }
  return { anyOf: [schema, { type: "null" }] };
}

function toOpenAiSchema(schema) {
  if (!isSchema(schema)) {
    throw new Error("AG Studio supplied an invalid JSON schema.");
  }

  if (Array.isArray(schema.anyOf)) {
    const branches = schema.anyOf.filter((branch) => !isUndefinedSentinel(branch));
    if (branches.length !== schema.anyOf.length) {
      if (branches.length === 0) {
        throw new Error("AG Studio supplied an empty optional schema.");
      }
      const { anyOf: _anyOf, ...rest } = schema;
      if (branches.length === 1 && isSchema(branches[0])) {
        return toOpenAiSchema({ ...branches[0], ...rest });
      }
      schema = { ...rest, anyOf: branches };
    }
  }

  if ("$ref" in schema) {
    return {
      $ref: schema.$ref,
      ...(schema.$defs ? { $defs: schema.$defs } : {}),
    };
  }

  const unsupportedKeywords = [
    "allOf",
    "not",
    "oneOf",
    "if",
    "then",
    "else",
    "prefixItems",
    "patternProperties",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
    "dependentSchemas",
    "dependentRequired",
    "contains",
  ];
  const unsupported = unsupportedKeywords.find((keyword) => keyword in schema);
  if (unsupported) {
    throw new Error(`OpenAI strict schemas do not support "${unsupported}".`);
  }

  const output = { ...schema };
  if (Array.isArray(output.anyOf)) {
    output.anyOf = output.anyOf.map(toOpenAiSchema);
  }
  if (isSchema(output.$defs)) {
    output.$defs = Object.fromEntries(
      Object.entries(output.$defs).map(([name, definition]) => [
        name,
        toOpenAiSchema(definition),
      ]),
    );
  }
  if ("const" in output) {
    const { const: literal, ...rest } = output;
    const normalized = { ...rest, enum: [literal] };
    if (normalized.type === undefined) {
      normalized.type =
        literal === null
          ? "null"
          : typeof literal === "number"
            ? Number.isInteger(literal)
              ? "integer"
              : "number"
            : typeof literal;
    }
    return toOpenAiSchema(normalized);
  }

  if (output.type === "object" || isSchema(output.properties)) {
    if (
      "additionalProperties" in output &&
      output.additionalProperties !== false
    ) {
      throw new Error(
        "OpenAI strict schemas require additionalProperties to be false.",
      );
    }
    const properties = isSchema(output.properties) ? output.properties : {};
    const required = new Set(
      Array.isArray(output.required) ? output.required : [],
    );
    output.properties = Object.fromEntries(
      Object.entries(properties).map(([name, property]) => {
        const normalized = toOpenAiSchema(property);
        return [
          name,
          required.has(name) ? normalized : makeNullable(normalized),
        ];
      }),
    );
    output.required = Object.keys(output.properties);
    output.additionalProperties = false;
  }
  if (isSchema(output.items)) {
    output.items = toOpenAiSchema(output.items);
  }
  return output;
}

function encodeStrictSchema(schema) {
  const normalized = toOpenAiSchema(schema);
  if (
    Array.isArray(normalized.anyOf) &&
    normalized.type !== "object" &&
    !isSchema(normalized.properties)
  ) {
    throw new Error("OpenAI strict schemas require an object at the root.");
  }
  return normalized;
}

function encodeTool(tool) {
  if (tool.kind === "provided") {
    return tool.provider;
  }

  return {
    type: "function",
    name: tool.name,
    description: tool.description,
    parameters: encodeStrictSchema(tool.parameters),
    strict: true,
  };
}

function encodeToolChoice(toolChoice) {
  if (!toolChoice || typeof toolChoice === "string") {
    return toolChoice;
  }

  return { type: "function", name: toolChoice.name };
}

function encodeResponseFormat(format) {
  if (format.type === "text") {
    return { format: { type: "text" } };
  }

  return {
    format: {
      type: "json_schema",
      name: format.name,
      description: format.description,
      schema: encodeStrictSchema(format.schema),
      strict: true,
    },
  };
}

function stripNullObjectProperties(value) {
  if (Array.isArray(value)) {
    return value.map(stripNullObjectProperties);
  }
  if (!isSchema(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, property]) => property !== null)
      .map(([name, property]) => [name, stripNullObjectProperties(property)]),
  );
}

function stripNullsFromArguments(argumentsText) {
  if (!argumentsText) {
    return argumentsText;
  }
  try {
    return JSON.stringify(stripNullObjectProperties(JSON.parse(argumentsText)));
  } catch {
    return argumentsText;
  }
}

function decodeOutputItem(item) {
  if (item.type === "message") {
    return {
      id: item.id,
      kind: "output",
      type: "message",
      role: "assistant",
      status: item.status || "completed",
      content: item.content.map((part) => {
        if (part.type === "output_text") {
          return {
            type: "text",
            text: part.text,
            annotations: [],
          };
        }
        return { type: "refusal", refusal: part.refusal };
      }),
    };
  }

  if (item.type === "function_call") {
    return {
      id: item.id,
      kind: "output",
      type: "function_call",
      callId: item.call_id,
      name: item.name,
      arguments: stripNullsFromArguments(item.arguments || ""),
      status: item.status,
    };
  }

  if (item.type === "reasoning") {
    return {
      id: item.id,
      kind: "output",
      type: "reasoning",
      summary: (item.summary || []).map((part) => ({
        type: "summary",
        text: part.text,
      })),
      content: item.content?.map((part) => ({
        type: "text",
        text: part.text,
      })),
    };
  }

  throw new Error(`Unsupported OpenAI output item: ${item.type}`);
}

function decodeResponse(response) {
  return {
    id: response.id,
    createdAt: (response.created_at || 0) * 1000,
    model: response.model,
    status: response.status,
    output: (response.output || []).map(decodeOutputItem),
    incompleteDetails: response.incomplete_details
      ? { reason: response.incomplete_details.reason }
      : undefined,
    error: response.error
      ? { code: response.error.code, message: response.error.message }
      : undefined,
    usage: response.usage
      ? {
          inputTokens: response.usage.input_tokens,
          outputTokens: response.usage.output_tokens,
          totalTokens: response.usage.total_tokens,
          reasoningTokens:
            response.usage.output_tokens_details?.reasoning_tokens,
          cachedInputTokens:
            response.usage.input_tokens_details?.cached_tokens,
        }
      : undefined,
  };
}

function* eventsForSseFrame(frame, state) {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") {
    return;
  }

  let event;
  try {
    event = JSON.parse(data);
  } catch (error) {
    throw new Error("The LLM proxy returned an invalid event.", { cause: error });
  }

  switch (event.type) {
    case "response.output_item.added": {
      const item = event.item;
      if (item.type === "message") {
        state.itemTypes.set(item.id, "message");
        yield { type: "TEXT_MESSAGE_START", messageId: item.id, role: "assistant" };
      } else if (item.type === "reasoning") {
        state.itemTypes.set(item.id, "reasoning");
        yield {
          type: "REASONING_MESSAGE_START",
          messageId: item.id,
          role: "reasoning",
        };
      } else if (item.type === "function_call") {
        state.itemTypes.set(item.id, "function_call");
        state.callIds.set(item.id, item.call_id);
        state.callArguments.set(item.call_id, "");
        yield {
          type: "TOOL_CALL_START",
          toolCallId: item.call_id,
          toolCallName: item.name,
        };
      }
      break;
    }
    case "response.output_item.done": {
      const item = event.item;
      const kind = state.itemTypes.get(item.id);
      if (kind === "message") {
        yield { type: "TEXT_MESSAGE_END", messageId: item.id };
      } else if (kind === "reasoning") {
        yield { type: "REASONING_MESSAGE_END", messageId: item.id };
      } else if (kind === "function_call") {
        const callId = state.callIds.get(item.id);
        if (callId) {
          const argumentsText = item.arguments || "";
          if (!state.callArguments.get(callId) && argumentsText) {
            yield {
              type: "TOOL_CALL_ARGS",
              toolCallId: callId,
              delta: argumentsText,
            };
          }
          yield { type: "TOOL_CALL_END", toolCallId: callId };
        }
      }
      break;
    }
    case "response.output_text.delta":
    case "response.refusal.delta":
      yield {
        type: "TEXT_MESSAGE_CONTENT",
        messageId: event.item_id,
        delta: event.delta,
      };
      break;
    case "response.reasoning_text.delta":
    case "response.reasoning_summary_text.delta":
      yield {
        type: "REASONING_MESSAGE_CONTENT",
        messageId: event.item_id,
        delta: event.delta,
      };
      break;
    case "response.function_call_arguments.delta": {
      const callId = state.callIds.get(event.item_id);
      if (callId) {
        state.callArguments.set(
          callId,
          `${state.callArguments.get(callId) || ""}${event.delta}`,
        );
        yield {
          type: "TOOL_CALL_ARGS",
          toolCallId: callId,
          delta: event.delta,
        };
      }
      break;
    }
    case "response.completed":
      state.response = decodeResponse(event.response);
      break;
    case "response.failed":
    case "response.incomplete":
      state.error = new Error(
        event.response?.error?.message || `OpenAI response ${event.type}.`,
      );
      break;
    case "error":
      state.error = new Error(event.message || "OpenAI request failed.");
      break;
    default:
      break;
  }
}

async function* readOpenAiEvents(response, state) {
  if (!response.body) {
    throw new Error("The LLM proxy response did not contain a stream.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    for (const frame of frames) {
      yield* eventsForSseFrame(frame, state);
    }
    if (done) {
      if (buffer.trim()) {
        yield* eventsForSseFrame(buffer, state);
      }
      return;
    }
  }
}

function createRequestBody(request) {
  const tools = (request.tools || []).map(encodeTool);
  return {
    input: request.input.map(encodeConversationItem),
    instructions: request.instructions,
    tools,
    tool_choice: encodeToolChoice(request.toolChoice),
    parallel_tool_calls: true,
    stream: true,
    store: false,
    text: encodeResponseFormat(request.responseFormat),
  };
}

export function createOpenAiBackendAdapter({ endpoint, getAccessToken }) {
  return {
    executeTurn(request, options = {}) {
      const state = {
        callArguments: new Map(),
        callIds: new Map(),
        itemTypes: new Map(),
      };
      let resolveComplete;
      let rejectComplete;
      const complete = new Promise((resolve, reject) => {
        resolveComplete = resolve;
        rejectComplete = reject;
      });
      void complete.catch(() => {});

      async function* streamEvents() {
        try {
          const response = await fetch(endpoint, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${getAccessToken()}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify(createRequestBody(request)),
            signal: options.signal,
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(
              body.detail ||
                body.error?.message ||
                `LLM request failed (${response.status}).`,
            );
          }

          yield* readOpenAiEvents(response, state);
          if (state.error) {
            throw state.error;
          }
          if (!state.response) {
            throw new Error("The LLM stream ended before a final response.");
          }
          resolveComplete(state.response);
        } catch (error) {
          rejectComplete(error);
        }
      }

      return {
        stream: {
          [Symbol.asyncIterator]: streamEvents,
        },
        complete,
      };
    },
  };
}
