"""Compile agent contracts with the actual CPU llama.cpp parser; no model needed.

Run separately from unit discovery with llama-cpp-python installed. A native
parser failure can terminate the process, so CI runs this as its own command.
"""

import json

import llama_cpp
from llama_cpp import LlamaGrammar

from common.protocol.agent_types import Decision, TurnPlan
from common.protocol.grammar_schema import grammar_schema
from cortex.cortex.inference import strict_schema


def main():
    for model in (TurnPlan, Decision):
        grammar = LlamaGrammar.from_json_schema(
            json.dumps(grammar_schema(strict_schema(model))), verbose=False
        )
        # A null vocabulary is sufficient for parsing; no token sampling occurs.
        sampler = llama_cpp.llama_sampler_init_grammar(
            None, grammar._grammar.encode(), b'root'
        )
        if not sampler:
            raise RuntimeError(f'{model.__name__}: native grammar compilation failed')
        llama_cpp.llama_sampler_free(sampler)
        print(f'{model.__name__}: native grammar compiled ({len(grammar._grammar)} characters)')


if __name__ == '__main__':
    main()
