"""`neuralosd transfer` — schema-similarity probe transfer.

    neuralosd transfer --from ~/instances/finacle_a --to ~/instances/finacle_b \
        --min-sim 0.6 --out ~/instances/finacle_b/transfer_proposal.json

A probe proven on one estate is PROPOSED for the schema-similar sibling:
shared probes are RECONFIRMED with the golden bank translated (re-oracle
flagged — the value was proven on ANOTHER estate), missing probes are
proposed with their definitions. A proposal is a proposal: nothing is
written into the target but the proposal document.
"""
import json
import os


def run(a):
    from ..loop import propose_transfer

    proposal = propose_transfer(os.path.abspath(a.source),
                                os.path.abspath(a.target),
                                min_sim=a.min_sim,
                                out_path=a.out)
    print(json.dumps(proposal, indent=2, ensure_ascii=False))
    n_prop = len(proposal["probes"])
    n_rec = len(proposal["reconfirmed"])
    print(f"\nschema similarity {proposal['similarity']}: "
          f"{n_prop} probe(s) proposed, {n_rec} reconfirmed with translated "
          f"golden cases")
    if n_prop or n_rec:
        print("review the proposal before applying anything — golden values "
              "must be RE-ORACLED on the target, never assumed")
