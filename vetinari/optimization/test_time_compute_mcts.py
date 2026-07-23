"""MCTS planning helpers for test-time compute."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable

from .test_time_compute_models import MCTSNode

logger = logging.getLogger(__name__)
_UCB1_DEFAULT_C: float = 1.414


class MCTSPlanner:
    """Tree search over candidate task decompositions.

    Uses UCB1 for selection, random expansion, rollout simulation,
    and backpropagation to find the best decomposition path.

    Args:
        exploration_weight: UCB1 exploration constant C. Higher = more
            exploration vs exploitation. Default sqrt(2) ≈ 1.414.
        max_iterations: Number of MCTS simulation loops.
        max_depth: Maximum depth of the search tree.
    """

    def __init__(
        self,
        exploration_weight: float = _UCB1_DEFAULT_C,
        max_iterations: int = 100,
        max_depth: int = 5,
    ) -> None:
        self._c = exploration_weight
        self._max_iter = max_iterations
        self._max_depth = max_depth

    def search(
        self,
        root_task: str,
        evaluate_fn: Callable[[str], float],
    ) -> list[str]:
        """Run MCTS and return the best decomposition path found.

        Args:
            root_task: The top-level task description to decompose.
            evaluate_fn: Callable that scores a task description string,
                returning a quality estimate in [0.0, 1.0].

        Returns:
            Ordered list of decomposition steps along the best path.
        """
        root = MCTSNode(state=root_task)

        for _ in range(self._max_iter):
            node = self._select(root)
            if node.visits > 0 and self._depth(node) < self._max_depth:
                node = self._expand(node)
            value = self._simulate(node, evaluate_fn)
            self._backpropagate(node, value)

        return self._get_best_path(root)

    def _select(self, node: MCTSNode) -> MCTSNode:
        """Descend tree using UCB1 until an unvisited or leaf node.

        Args:
            node: Starting node for selection.

        Returns:
            Selected node for expansion/simulation.
        """
        while node.children:
            # If any child is unvisited, select it
            unvisited = [c for c in node.children if c.visits == 0]
            if unvisited:
                return unvisited[0]
            node = max(node.children, key=lambda c: self._ucb1(c))
        return node

    def _ucb1(self, node: MCTSNode) -> float:
        """Compute UCB1 score for a node.

        Args:
            node: The node to score.

        Returns:
            UCB1 value (exploitation + exploration bonus).
        """
        if node.visits == 0:
            return float("inf")
        parent_visits = node.parent.visits if node.parent else node.visits
        exploitation = node.value
        exploration = self._c * math.sqrt(math.log(max(1, parent_visits)) / node.visits)
        return exploitation + exploration

    def _expand(self, node: MCTSNode) -> MCTSNode:
        """Generate child nodes by decomposing the current task.

        Args:
            node: The node to expand.

        Returns:
            A newly created child node.
        """
        if not node.children:
            subtasks = self._decompose_step(node.state)
            for subtask in subtasks:
                child = MCTSNode(state=subtask, parent=node, action=subtask)
                node.children.append(child)

        # Return first unvisited child, or the first child if all visited
        for child in node.children:
            if child.visits == 0:
                return child
        return node.children[0]

    @staticmethod
    def _simulate(
        node: MCTSNode,
        evaluate_fn: Callable[[str], float],
    ) -> float:
        """Rollout: evaluate the current node's task quality.

        Args:
            node: Node to evaluate.
            evaluate_fn: Quality scoring function.

        Returns:
            Quality estimate in [0.0, 1.0].
        """
        try:
            score = evaluate_fn(node.state)
            return float(max(0.0, min(1.0, score)))
        except Exception:
            logger.warning("[MCTSPlanner] evaluate_fn raised; defaulting to 0.5")
            return 0.5

    @staticmethod
    def _backpropagate(node: MCTSNode, value: float) -> None:
        """Propagate the simulation result up to the root.

        Args:
            node: Leaf node where simulation completed.
            value: Reward to propagate.
        """
        current: MCTSNode | None = node
        while current is not None:
            current.visits += 1
            current.total_value += value
            current = current.parent

    @staticmethod
    def _decompose_step(task: str) -> list[str]:
        """Heuristic decomposition: split a task into 2-4 subtasks.

        This is MCTS's **action-proposal policy** — it generates the set of
        candidate child states when `_expand` is called on a node. It is NOT
        the search algorithm itself; the UCB1 selection in `_select`, the
        tree expansion in `_expand`, the rollout evaluation in `_simulate`,
        and the value backpropagation in `_backpropagate` together constitute
        the search. A domain-specific proposal policy (regex, heuristic
        grammar, or a neural policy head in stronger variants) is a standard
        MCTS component — see ADR-0100 for evidence.

        Uses pattern matching to identify common decomposition points
        (and, then, also, first/second/finally). Falls back to sentence
        splitting or generic subtasks if no patterns are found.

        Args:
            task: Task description to decompose.

        Returns:
            List of 2-4 subtask strings.
        """
        # Try splitting on conjunction keywords
        conjunctions = re.split(r"\b(?:and then|and also|, then|; then|; and)\b", task, flags=re.IGNORECASE)
        if len(conjunctions) >= 2:
            return [s.strip() for s in conjunctions[:4] if s.strip()]

        # Try splitting on sentence boundaries
        sentences = re.split(r"(?<=[.!?])\s+", task.strip())
        if len(sentences) >= 2:
            return [s.strip() for s in sentences[:4] if s.strip()]

        # Generic subtask generation based on task length
        words = task.split()
        if len(words) <= 5:
            return [f"Prepare for: {task}", f"Execute: {task}"]
        mid = len(words) // 2
        part1 = " ".join(words[:mid])
        part2 = " ".join(words[mid:])
        return [f"Phase 1 — {part1}", f"Phase 2 — {part2}"]

    @staticmethod
    def _get_best_path(root: MCTSNode) -> list[str]:
        """Trace the highest-value path from root to deepest best child.

        Args:
            root: Root node of the MCTS tree.

        Returns:
            List of action strings along the best path (excludes root action).
        """
        path: list[str] = []
        node = root
        while node.children:
            best = max(node.children, key=lambda c: c.value)
            if best.visits == 0:
                break
            if best.action:
                path.append(best.action)
            node = best
        return path or [root.state]

    @staticmethod
    def _depth(node: MCTSNode) -> int:
        """Compute depth of a node from the root.

        Args:
            node: Node to measure.

        Returns:
            Integer depth (root = 0).
        """
        depth = 0
        current: MCTSNode | None = node.parent
        while current is not None:
            depth += 1
            current = current.parent
        return depth
