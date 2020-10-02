DSA110 Shell repo. Use 'myrepo' to clone down all repos for this project.
- Clone this repo: 'git clone git@github.com/dsa110/dsa110-shell dsa110
- Add <path-to-dsa110/.mrconfig> to ~/.mrtrust
- cd dsa110
- mr checkout

All DSA110 repos will be using Symantic Versioning. Info at
https://semver.org/ will explain what it is and why it's useful. These
versions do not need to be placed on user dev branches but should be placed
on every commit on master and the main development branch. These two
branches track developer features/bug fixes. Master being used for production
rollouts and the development branch for feature integration.

How to create a new git repo with 'main' instead of a master branch
- git init
- git checkout -b main
- git add .
- git commit -s -m init

How to convert a git repo's master branch to 'main'
- git checkout -b main master
- git push origin main
- git branch -D master

If you have access to the origin's repo then on origin in the repo:
-  git symbolic-ref HEAD refs/heads/main
- back on local machine: git push origin :master

If using github or gitolite, Use UI tools to switch HEAD to point to main
- then back on local repo: git push origin :master


