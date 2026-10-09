# 2048 in a browser page

## Context

You start with an empty folder. There is no starter code, no framework and no build setup.

## End goal

Build the game 2048 as a web page that opens straight from the file system. A person double-clicks `index.html` and can play.

## Requirements

1. The entry point is `index.html` in your folder. You may add JavaScript and CSS files next to it. The page must work when opened as a local file (a `file://` address) with no build step, no server and no network access. Do not load anything from the internet.
2. The game is the usual 2048 on a 4 by 4 board. Tiles slide as far as they can in the direction of a move. Two equal tiles that collide merge into one tile of double the value. A tile made by a merge cannot merge again in the same move, and in a row of tiles the pair nearest the wall merges first. For example, sliding left turns `2 2 2 2` into `4 4 0 0`, and `2 2 2 0` into `4 2 0 0`.
3. Your score goes up, on each merge, by the value of the new tile. The score is always visible on the page.
4. The arrow keys play the game: left, right, up and down.
5. After a move that changed the board, one new tile appears in a random empty cell. It is a 2 or a 4. If a move changed nothing, no tile appears and nothing else changes.
6. A new game starts with two tiles, each a 2 or a 4, and a score of zero.
7. When no move is possible (the board is full and no two neighbouring tiles are equal), the page shows the words Game over.
8. A visible control on the page, such as a button, starts a new game at any time, including after the game is over.
9. Show the tiles as ordinary page elements, such as `div` elements, with the number written as text inside each tile. Do not draw the board on a canvas.

## Test interface

The page must expose an object named `window.game`, so that the game can be checked by a script. It has these functions. Directions are the strings `"left"`, `"right"`, `"up"` and `"down"`. A board is an array of 4 rows of 4 numbers, from the top row to the bottom row, and 0 means an empty cell.

- `reset(seed)` starts a new game from a number `seed`. The same seed must always give the same starting board, and the same sequence of moves after it must give the same new tiles. Calling `reset()` with no argument starts a game with a random seed.
- `load(board)` replaces the board with the given one, sets the score to zero, clears any game over state, and redraws the page.
- `move(direction)` does the same as pressing that arrow key, and returns `true` if the board changed and `false` if it did not.
- `board()` returns the current board as an array of 4 rows of 4 numbers.
- `score()` returns the current score as a number.
- `over()` returns `true` if no move is possible, otherwise `false`.

The displayed board and score must always match what these functions return, whether the board changed through a key press, through `move`, or through `load`. After `load` of a board with no possible move, `over()` is `true` and the page shows Game over.

## Constraints

You are the whole system for this task. You may use your own sub-agents, workers, or parallel sessions the way you normally would. Everything they do counts as your work and your cost. Do not get help from outside your own system: no human, and no other contestant.

## References

None are supplied and none are needed.

## Verification expected

Open the page in a real browser, play it, and check it yourself. Test the merge rules, the game over state and the restart control. Fix what you find.

## Final response

State what you built, which files are in the folder, and how you checked it. Say what you could not check. Do not claim that something works unless you ran it.
