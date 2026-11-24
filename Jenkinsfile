// One multibranch job. Every branch and pull request is validated and built; only the develop branch publishes.
def pipeline
node {
    checkout scm
    pipeline = load('ci/pipelines/dev.groovy')
}
pipeline.call()
